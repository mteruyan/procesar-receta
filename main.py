import functions_framework
import os
import json
import requests
import re
from datetime import datetime, timezone, timedelta
from google.cloud import documentai, storage
import vertexai
from vertexai.generative_models import GenerativeModel
import pg8000
from google.cloud.sql.connector import Connector
import time
import random
from google.api_core.exceptions import ResourceExhausted
import hashlib
import base64

from google import genai
from google.genai import types as genai_types
from google.genai.errors import ClientError, ServerError

# ──────────────────────────────────────────────────────────────
# CONFIGURACIÓN — ajusta estos valores según tu proyecto
# ──────────────────────────────────────────────────────────────
PROJECT_ID       = "sanitas-recetas-prod"
LOCATION         = "us"                          # región de tu procesador Document AI
PROCESSOR_ID     = "6837fb55f199826a"            # ID de tu procesador Document AI
BUCKET_NAME      = os.environ.get("BUCKET_NAME")    # bucket Cloud Storage
DB_NAME          = os.environ.get("DB_NAME")                     # base de datos PostgreSQL
DB_USER          = os.environ.get("DB_USER")
DB_PASS          = os.environ.get("DB_PASS")
INSTANCE_CONN    = os.environ.get("INSTANCE_CONN")
#GEMINI_MODEL     = "gemini-2.5-flash-lite"
GEMINI_MODEL     = "gemini-3.1-flash-lite"
UMBRAL_CONFIANZA = 0.87                           # por debajo → manuscrito → atención humana
LIMA_TZ          = timezone(timedelta(hours=-5)) # UTC-5, sin DST

MENSAJE_PROGRAMAS = (
    "💙 *¡Queremos acompañarte en el cuidado de tu salud!* 🩺✨\n\n"
    "Sabemos que seguir un tratamiento puede ser todo un reto. Por eso, en *Sanitas* "
    "tenemos *programas de salud* que te acompañan de manera cercana, con:\n\n"
    "🔔 Recordatorios para no olvidar lo importante\n"
    "💡 Tips y recomendaciones para tu bienestar\n"
    "🤝 Seguimiento cercano durante tu tratamiento\n"
    "✨ ¡Y lo mejor! Es sin costo adicional para ti.\n\n"
    "👉 ¿Damos el primer paso juntos? 💙\n\n"
    "📲 Desde el menú principal, ingresa a:\n"
    "➡️ *Consultas / Programas de salud*\n\n"
    "🌟 Estamos contigo en cada paso."
)

MENSAJE_SIN_PROGRAMAS = "⏳ espere un momento por favor …"
TIPO_CLIENTE_PROGRAMAS = "PROGRAMAS_SALUD"
WHATSAPP_DESTINO_PROGRAMAS = os.environ.get("WHATSAPP_DESTINO_PROGRAMAS", "51955973936")


# MIME types soportados por Document AI
MIME_PDF  = "application/pdf"
MIME_JPEG = "image/jpeg"
MIME_PNG  = "image/png"
MIME_WEBP = "image/webp"
MIME_TIFF = "image/tiff"
MIME_GIF  = "image/gif"

# ──────────────────────────────────────────────────────────────
# SEGURIDAD DASHBOARD — clave por query string (?key=...)
# ──────────────────────────────────────────────────────────────
def validar_key_dashboard(request) -> bool:
    key_recibida = request.args.get("key", "")
    key_esperada = os.environ.get("DASHBOARD_RECETAS_KEY", "")
    return bool(key_esperada) and key_recibida == key_esperada


# ──────────────────────────────────────────────────────────────
# SEGURIDAD — API Key en cabecera (sin expiración)
# Uso: Authorization: ApiKey TU_CLAVE
# Configurar API_KEY como variable de entorno en Cloud Run
# ──────────────────────────────────────────────────────────────
def validar_api_key(request) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("ApiKey "):
        return False
    key = auth.split(" ", 1)[1].strip()
    return key == os.environ.get("API_KEY", "")

# ──────────────────────────────────────────────────────────────
# HEADERS CORS
# ──────────────────────────────────────────────────────────────
HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Content-Type":                 "application/json"
}

# ──────────────────────────────────────────────────────────────
# CONEXIÓN A BASE DE DATOS
# ──────────────────────────────────────────────────────────────
def get_db_connection():
    connector = Connector()
    conn = connector.connect(
        INSTANCE_CONN, "pg8000",
        user=DB_USER, password=DB_PASS, db=DB_NAME
    )
    cur = conn.cursor()
    cur.execute("SET TIME ZONE 'America/Lima'")
    conn.commit()
    cur.close()
    return conn, connector

# ──────────────────────────────────────────────────────────────
# CONEXIÓN A BASE DE DATOS DE SOFI-APIS (misma instancia Cloud SQL,
# distinta base de datos) — usada solo para registrar sms_aviso
# ──────────────────────────────────────────────────────────────
def get_db_connection_sofi_whatsapp():
    connector = Connector()
    conn = connector.connect(
        INSTANCE_CONN, "pg8000",
        user=DB_USER, password=DB_PASS, db="sofi_whatsapp"
    )
    cur = conn.cursor()
    cur.execute("SET TIME ZONE 'America/Lima'")
    conn.commit()
    cur.close()
    return conn, connector


def _generar_code_click_programa(celular: str) -> str:
    raw = f"{celular}{time.time()}".encode()
    h = hashlib.md5(raw).digest()
    return base64.urlsafe_b64encode(h)[:7].decode()

# ──────────────────────────────────────────────────────────────
# BÚSQUEDA EN PETITORIO — accent-insensitive, match parcial sobre
# principio_activo contra sofi_petitorio.descripcion
# ──────────────────────────────────────────────────────────────
def verificar_petitorio(cur, principio_activo: str) -> bool:
    if not principio_activo:
        return False
    cur.execute("""
        SELECT EXISTS (
            SELECT 1 FROM sofi_petitorio
            WHERE unaccent(upper(descripcion)) ~ (
                '(^|[^A-Z])' || unaccent(upper(%s)) || '($|[^A-Z])'
            )
        )
    """, (principio_activo,))
    return cur.fetchone()[0]

# ──────────────────────────────────────────────────────────────
# BÚSQUEDA EN PEAS — verifica si el codigo CIE-10 del diagnostico
# esta cubierto por el Plan Esencial de Aseguramiento en Salud
# ──────────────────────────────────────────────────────────────
def verificar_peas(cur, codigo_cie10: str) -> bool:
    codigo_norm = normalizar_codigo_cie10(codigo_cie10)
    if not codigo_norm:
        return False
    cur.execute("""
        SELECT EXISTS (
            SELECT 1 FROM sofi_dx_peas
            WHERE regexp_replace(
                      regexp_replace(upper(trim(codigo_cie10)), '[^A-Z0-9]', '', 'g'),
                      'X$', ''
                  ) = %s
        )
    """, (codigo_norm,))
    return cur.fetchone()[0]

# ──────────────────────────────────────────────────────────────
# BÚSQUEDA EN PROGRAMAS — devuelve la lista de programas (unicos)
# a los que califica la receta segun sus diagnosticos, con el
# codigo/descripcion del diagnostico que hizo calificar a cada uno
# ──────────────────────────────────────────────────────────────
def obtener_programas_receta(cur, diagnosticos: list) -> list:
    programas_encontrados = {}
    for diag in diagnosticos:
        codigo_norm = normalizar_codigo_cie10(diag.get("codigo", ""))
        if not codigo_norm:
            continue
        cur.execute("""
            SELECT DISTINCT programa, cie_10, descripcion
            FROM sofi_programa_diagnosticos
            WHERE regexp_replace(
                      regexp_replace(upper(trim(cie_10)), '[^A-Z0-9]', '', 'g'),
                      'X$', ''
                  ) = %s
        """, (codigo_norm,))
        for programa, cie_10, descripcion in cur.fetchall():
            if programa not in programas_encontrados:
                programas_encontrados[programa] = {
                    "programa": programa,
                    "codigo_cie10": cie_10,
                    "descripcion_diagnostico": descripcion
                }
    return list(programas_encontrados.values())


# ──────────────────────────────────────────────────────────────
# GUARDAR EN pacientes_programas — una fila por cada programa
# nuevo (no duplica si el DNI ya esta registrado en ese programa)
# ──────────────────────────────────────────────────────────────
def guardar_pacientes_programas(
    cur, receta_id: int, celular: str, analisis: dict, programas: list, now_lima
):
    dni_paciente = (analisis.get("dni_paciente") or "").strip()

    for prog in programas:
        if dni_paciente:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM pacientes_programas
                    WHERE dni_paciente = %s AND programa = %s
                )
            """, (dni_paciente, prog["programa"]))
            if cur.fetchone()[0]:
                continue  # ya esta registrado en ese programa, no duplicar

        cur.execute("""
            INSERT INTO pacientes_programas (
                receta_id, celular, dni_paciente, nombre_paciente,
                aseguradora, programa, codigo_cie10, descripcion_diagnostico,
                fecha_registro
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            receta_id, celular, dni_paciente or None,
            analisis.get("nombre_paciente"),
            analisis.get("aseguradora"),
            prog["programa"], prog["codigo_cie10"], prog["descripcion_diagnostico"],
            now_lima
        ))

# ──────────────────────────────────────────────────────────────
# BÚSQUEDA EN PETITORIO — accent-insensitive, con desglose por
# componente cuando el principio_activo viene combinado ("/" o "+")
# ──────────────────────────────────────────────────────────────
def verificar_petitorio_detalle(cur, principio_activo: str) -> list:
    if not principio_activo:
        return []

    componentes = [c.strip() for c in re.split(r"[/+]", principio_activo) if c.strip()]
    detalle     = []

    for componente in componentes:
        cur.execute("""
            SELECT
                bool_or(TRUE)                        AS encontrado,
                bool_or(requiere_carta_garantia)      AS con_carta_garantia,
                bool_or(requiere_cuida)               AS con_cuida
            FROM sofi_petitorio
            WHERE unaccent(upper(%s)) ~ (
                    '(^|[^A-Z])' || unaccent(upper(descripcion)) || '($|[^A-Z])'
                  )
               OR unaccent(upper(descripcion)) ~ (
                    '(^|[^A-Z])' || unaccent(upper(%s)) || '($|[^A-Z])'
                  )
        """, (componente, componente))
        fila       = cur.fetchone()
        encontrado = bool(fila[0])

        # Fallback: similitud difusa para typos/truncamientos de Gemini
        # (ej. "ESOMEPRAZO" vs "ESOMEPRAZOL")
        if not encontrado:
            cur.execute("""
                SELECT
                    bool_or(TRUE) AS encontrado,
                    bool_or(requiere_carta_garantia) AS con_carta_garantia,
                    bool_or(requiere_cuida) AS con_cuida
                FROM sofi_petitorio
                WHERE similarity(unaccent(upper(descripcion)), unaccent(upper(%s))) > 0.6
            """, (componente,))
            fila       = cur.fetchone()
            encontrado = bool(fila[0]) if fila else False

        con_carta_garantia = bool(fila[1]) if encontrado else False
        con_cuida          = bool(fila[2]) if encontrado else False
        detalle.append({
            "componente": componente,
            "en_petitorio": encontrado,
            "requiere_carta_garantia": con_carta_garantia,
            "requiere_cuida": con_cuida
        })

    return detalle

# ──────────────────────────────────────────────────────────────
# DETECCIÓN DE MIME TYPE desde la URL
# ──────────────────────────────────────────────────────────────
def detectar_mime_type(ruta: str) -> str:
    ruta_lower = ruta.lower().split("?")[0]
    if ruta_lower.endswith(".pdf"):                          return MIME_PDF
    if ruta_lower.endswith(".png"):                          return MIME_PNG
    if ruta_lower.endswith(".webp"):                         return MIME_WEBP
    if ruta_lower.endswith(".tiff") or ruta_lower.endswith(".tif"): return MIME_TIFF
    if ruta_lower.endswith(".gif"):                          return MIME_GIF
    if ruta_lower.endswith(".jpg") or ruta_lower.endswith(".jpeg"): return MIME_JPEG
    # Si la extensión no es clara, consultar el servidor
    try:
        resp = requests.head(ruta, timeout=10, allow_redirects=True)
        ct   = resp.headers.get("Content-Type", MIME_JPEG)
        return ct.split(";")[0].strip()
    except Exception:
        return MIME_JPEG  # fallback seguro


HEADERS_HTML = {
    "Content-Type": "text/html; charset=utf-8"
}

# ──────────────────────────────────────────────────────────────
# NORMALIZACIÓN DE CÓDIGO CIE-10 — corrige confusiones de OCR
# (letra/digito en la primera posicion) y sufijos comodin ".X"/"X"
# ──────────────────────────────────────────────────────────────
def normalizar_codigo_cie10(codigo: str) -> str:
    if not codigo:
        return ""
    # quitar todo lo que no sea letra o numero (incluye puntos, guiones, espacios)
    c = re.sub(r'[^A-Za-z0-9]', '', codigo).upper()
    if not c:
        return ""
    # un codigo CIE-10 siempre inicia con letra; si el OCR leyo un digito
    # en la primera posicion, corregir confusiones comunes (I<->1, O<->0)
    if c[0].isdigit():
        correcciones = {'1': 'I', '0': 'O'}
        c = correcciones.get(c[0], c[0]) + c[1:]
    # quitar sufijo comodin final "X" (ej. "I10X" -> "I10", "B79X" -> "B79")
    if len(c) > 3 and c.endswith('X'):
        c = c[:-1]
    return c

# ──────────────────────────────────────────────────────────────
# DASHBOARD DE ESTADISTICAS — GET /dashboard-recetas?key=...
# ──────────────────────────────────────────────────────────────
def handle_dashboard_recetas(request):
    import html as html_lib

    if request.method != "GET":
        return (json.dumps({"error": "Metodo no permitido"}), 405, HEADERS)

    if not validar_key_dashboard(request):
        return ("<h1>403 - Acceso denegado</h1>", 403, HEADERS_HTML)

    # ── Rango de fechas (default: mes actual) ─────────────────
    now_lima = datetime.now(LIMA_TZ)
    fecha_fin_default    = now_lima.date()
    fecha_inicio_default = now_lima.date().replace(day=1)

    # ── Rango de fechas (default: ultimos 30 dias) ────────────
    #now_lima = datetime.now(LIMA_TZ)
    #fecha_fin_default    = now_lima.date()
    #fecha_inicio_default = (now_lima - timedelta(days=30)).date()

    fecha_inicio_str = request.args.get("fecha_inicio", str(fecha_inicio_default))
    fecha_fin_str    = request.args.get("fecha_fin", str(fecha_fin_default))

    try:
        fecha_inicio = datetime.strptime(fecha_inicio_str, "%Y-%m-%d")
        fecha_fin    = datetime.strptime(fecha_fin_str, "%Y-%m-%d") + timedelta(days=1)  # inclusive
    except ValueError:
        fecha_inicio = datetime.combine(fecha_inicio_default, datetime.min.time())
        fecha_fin    = datetime.combine(fecha_fin_default, datetime.min.time()) + timedelta(days=1)
        fecha_inicio_str = str(fecha_inicio_default)
        fecha_fin_str    = str(fecha_fin_default)

    key = request.args.get("key", "")

    conn, connector = get_db_connection()
    cur = conn.cursor()
    try:
        # ── KPIs generales ─────────────────────────────────────
        cur.execute("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE requiere_revision) AS total_revision,
                ROUND(AVG(confianza_ocr)::numeric, 3) AS promedio_confianza,
                COUNT(*) FILTER (WHERE estado = 'aprobado')  AS total_aprobado,
                COUNT(*) FILTER (WHERE estado = 'rechazado') AS total_rechazado,
                COUNT(*) FILTER (WHERE estado = 'revision')  AS total_estado_revision,
                COUNT(*) FILTER (WHERE requiere_revision AND confianza_ocr < 0.87)  AS total_por_ocr_bajo,
                COUNT(*) FILTER (WHERE requiere_revision AND confianza_ocr >= 0.87) AS total_por_motivo_ia
            FROM recetas
            WHERE created_at >= %s AND created_at < %s
        """, (fecha_inicio, fecha_fin))
        row = cur.fetchone()
        total_recetas          = row[0] or 0
        total_revision_humana  = row[1] or 0
        promedio_confianza     = float(row[2]) if row[2] is not None else 0.0
        total_aprobado         = row[3] or 0
        total_rechazado        = row[4] or 0
        total_estado_revision  = row[5] or 0
        total_por_ocr_bajo     = row[6] or 0
        total_por_motivo_ia    = row[7] or 0
        pct_revision_humana    = round((total_revision_humana / total_recetas) * 100, 1) if total_recetas else 0
        pct_por_ocr_bajo       = round((total_por_ocr_bajo / total_revision_humana) * 100, 1) if total_revision_humana else 0
        pct_por_motivo_ia      = round((total_por_motivo_ia / total_revision_humana) * 100, 1) if total_revision_humana else 0

        # ── % medicamentos con carta de garantia ───────────────
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE rm.requiere_carta_garantia) AS con_carta,
                COUNT(*) AS total_meds
            FROM receta_medicamentos rm
            JOIN recetas r ON r.id = rm.receta_id
            WHERE r.created_at >= %s AND r.created_at < %s
        """, (fecha_inicio, fecha_fin))
        con_carta, total_meds = cur.fetchone()
        con_carta   = con_carta or 0
        total_meds  = total_meds or 0
        pct_carta   = round((con_carta / total_meds) * 100, 1) if total_meds else 0

        # ── Top 15 clinicas (normalizando nombre para evitar duplicados) ─
        cur.execute("""
            WITH normalizado AS (
                SELECT
                    upper(trim(regexp_replace(
                        COALESCE(NULLIF(nombre_clinica, ''), 'SIN IDENTIFICAR'),
                        '\\s+', ' ', 'g'
                    ))) AS clinica,
                    COALESCE(NULLIF(ruc_clinica, ''), '-') AS ruc,
                    requiere_revision
                FROM recetas
                WHERE created_at >= %s AND created_at < %s
            )
            SELECT
                clinica,
                MODE() WITHIN GROUP (ORDER BY ruc) AS ruc,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE requiere_revision) AS total_revision
            FROM normalizado
            GROUP BY clinica
            ORDER BY total DESC
            LIMIT 15
        """, (fecha_inicio, fecha_fin))
        top_clinicas = cur.fetchall()

        # ── Top 15 medicamentos por nombre ──────────────────────
        cur.execute("""
            SELECT rm.nombre_medicamento, COUNT(*) AS total
            FROM receta_medicamentos rm
            JOIN recetas r ON r.id = rm.receta_id
            WHERE r.created_at >= %s AND r.created_at < %s
              AND rm.nombre_medicamento IS NOT NULL AND rm.nombre_medicamento <> ''
            GROUP BY rm.nombre_medicamento
            ORDER BY total DESC
            LIMIT 15
        """, (fecha_inicio, fecha_fin))
        top_medicamentos = cur.fetchall()

        # ── Top 15 principios activos (componentes separados) ──
        cur.execute("""
            SELECT trim(componente) AS principio, COUNT(*) AS total
            FROM (
                SELECT unnest(regexp_split_to_array(upper(rm.principio_activo), '[/+]')) AS componente
                FROM receta_medicamentos rm
                JOIN recetas r ON r.id = rm.receta_id
                WHERE r.created_at >= %s AND r.created_at < %s
                  AND rm.principio_activo IS NOT NULL AND rm.principio_activo <> ''
            ) sub
            WHERE trim(componente) <> ''
            GROUP BY principio
            ORDER BY total DESC
            LIMIT 15
        """, (fecha_inicio, fecha_fin))
        top_principios = cur.fetchall()

        # ── Top 15 diagnosticos ──────────────────────────────────
        # codigo_base: limpia caracteres invalidos, conserva el punto (para variantes de display).
        # codigo_corr: corrige confusion de OCR/Gemini I<->1 y O<->0 SOLO en la 1ra posicion.
        # codigo_key: clave de agrupacion — quita el punto y el sufijo comodin "X" final,
        # igual que normalizar_codigo_cie10(), para que "I10.X" e "I10" caigan en el mismo grupo.
        cur.execute("""
            WITH diag_base AS (
                SELECT
                    rd.descripcion,
                    upper(trim(regexp_replace(rd.codigo, '[^A-Za-z0-9.]', '', 'g'))) AS codigo_base
                FROM receta_diagnosticos rd
                JOIN recetas r ON r.id = rd.receta_id
                WHERE r.created_at >= %s AND r.created_at < %s
                  AND rd.codigo IS NOT NULL AND rd.codigo <> ''
            ),
            diag_corr AS (
                SELECT
                    descripcion,
                    codigo_base,
                    CASE
                        WHEN left(codigo_base,1) = '1' THEN 'I' || substring(codigo_base from 2)
                        WHEN left(codigo_base,1) = '0' THEN 'O' || substring(codigo_base from 2)
                        ELSE codigo_base
                    END AS codigo_corr
                FROM diag_base
            ),
            diag_norm AS (
                SELECT
                    descripcion,
                    codigo_corr,
                    trim(regexp_replace(descripcion, '\\s*\\(N?O?\\s*PEAS\\)\\s*$', '', 'i')) AS descripcion_limpia,
                    (regexp_replace(codigo_corr, '[^A-Z0-9]', '', 'g')) AS codigo_sin_punto
                FROM diag_corr
            ),
            diag_key AS (
                SELECT
                    descripcion_limpia,
                    codigo_corr,
                    CASE
                        WHEN length(codigo_sin_punto) > 3 AND right(codigo_sin_punto, 1) = 'X'
                            THEN left(codigo_sin_punto, length(codigo_sin_punto) - 1)
                        ELSE codigo_sin_punto
                    END AS codigo_key
                FROM diag_norm
            )
            SELECT
                MODE() WITHIN GROUP (ORDER BY codigo_corr)        AS codigo,
                MODE() WITHIN GROUP (ORDER BY descripcion_limpia) AS descripcion,
                COUNT(*) AS total,
                EXISTS (
                    SELECT 1 FROM sofi_dx_peas spd
                    WHERE regexp_replace(
                              regexp_replace(upper(trim(spd.codigo_cie10)), '[^A-Z0-9]', '', 'g'),
                              'X$', ''
                          ) = codigo_key
                ) AS es_peas
            FROM diag_key
            GROUP BY codigo_key
            ORDER BY total DESC
            LIMIT 15
        """, (fecha_inicio, fecha_fin))
        top_diagnosticos = cur.fetchall()

        # ── Top motivos de revision detectados por Gemini ───────
        cur.execute("""
            SELECT COALESCE(NULLIF(motivo_revision, ''), 'SIN MOTIVO ESPECIFICADO') AS motivo, COUNT(*) AS total
            FROM recetas
            WHERE created_at >= %s AND created_at < %s
              AND requiere_revision = TRUE
              AND confianza_ocr >= 0.87
            GROUP BY motivo
            ORDER BY total DESC
            LIMIT 8
        """, (fecha_inicio, fecha_fin))
        top_motivos_revision = cur.fetchall()

        # ── Evolucion diaria de recetas ──────────────────────────
        cur.execute("""
            SELECT DATE(created_at) AS dia, COUNT(*) AS total
            FROM recetas
            WHERE created_at >= %s AND created_at < %s
            GROUP BY dia
            ORDER BY dia
        """, (fecha_inicio, fecha_fin))
        evolucion_diaria = cur.fetchall()

        # ── Distribucion de medicamentos segun Petitorio (dato objetivo) ─
        cur.execute("""
            SELECT
                CASE
                    WHEN rm.requiere_carta_garantia THEN 'En petitorio (con carta)'
                    WHEN rm.en_petitorio THEN 'En petitorio'
                    ELSE 'Fuera de petitorio'
                END AS categoria,
                COUNT(*) AS total
            FROM receta_medicamentos rm
            JOIN recetas r ON r.id = rm.receta_id
            WHERE r.created_at >= %s AND r.created_at < %s
            GROUP BY categoria
        """, (fecha_inicio, fecha_fin))
        distribucion_petitorio = cur.fetchall()

        # ── Top 15 duraciones de tratamiento mas frecuentes (normalizado) ─
        cur.execute(r"""
            WITH duraciones_norm AS (
                SELECT
                    CASE
                        WHEN unaccent(upper(trim(rm.duracion))) ~ '^[0-9]+(\.[0-9]+)?$'
                            THEN regexp_replace(unaccent(upper(trim(rm.duracion))), '\.0+$', '') || ' DIAS'
                        ELSE regexp_replace(
                                regexp_replace(
                                    unaccent(upper(trim(rm.duracion))),
                                    '(\d+)\.0+(\s|$)', '\1\2', 'g'
                                ),
                                '\s+', ' ', 'g'
                            )
                    END AS duracion
                FROM receta_medicamentos rm
                JOIN recetas r ON r.id = rm.receta_id
                WHERE r.created_at >= %s AND r.created_at < %s
                AND rm.duracion IS NOT NULL AND rm.duracion <> ''
            )
            SELECT duracion, COUNT(*) AS total
            FROM duraciones_norm
            GROUP BY duracion
            ORDER BY total DESC
            LIMIT 15
        """, (fecha_inicio, fecha_fin))
        top_duraciones = cur.fetchall()

        # ── Top 15 medicamentos solicitados que NO estan en petitorio ─
        cur.execute("""
            SELECT rm.nombre_medicamento, COUNT(*) AS total
            FROM receta_medicamentos rm
            JOIN recetas r ON r.id = rm.receta_id
            WHERE r.created_at >= %s AND r.created_at < %s
              AND rm.en_petitorio = FALSE
              AND rm.nombre_medicamento IS NOT NULL AND rm.nombre_medicamento <> ''
            GROUP BY rm.nombre_medicamento
            ORDER BY total DESC
            LIMIT 15
        """, (fecha_inicio, fecha_fin))
        top_no_petitorio = cur.fetchall()

        # ── Top 15 pacientes que mas solicitan el servicio ──────
        cur.execute("""
            SELECT
                MODE() WITHIN GROUP (ORDER BY upper(trim(nombre_paciente))) AS paciente,
                dni_paciente,
                COUNT(*) AS total
            FROM recetas
            WHERE created_at >= %s AND created_at < %s
              AND dni_paciente IS NOT NULL AND dni_paciente <> ''
            GROUP BY dni_paciente
            ORDER BY total DESC
            LIMIT 15
        """, (fecha_inicio, fecha_fin))
        top_pacientes = cur.fetchall()

        # ── Top 15 medicamentos que requieren carta de garantia ─
        cur.execute("""
            SELECT rm.nombre_medicamento, COUNT(*) AS total
            FROM receta_medicamentos rm
            JOIN recetas r ON r.id = rm.receta_id
            WHERE r.created_at >= %s AND r.created_at < %s
            AND rm.requiere_carta_garantia = TRUE
            AND rm.nombre_medicamento IS NOT NULL AND rm.nombre_medicamento <> ''
            GROUP BY rm.nombre_medicamento
            ORDER BY total DESC
            LIMIT 15
        """, (fecha_inicio, fecha_fin))
        top_carta_garantia = cur.fetchall()

        # ── Top 15 diagnosticos del programa KRYSTAL mas frecuentes ─
        cur.execute("""
            SELECT spd.cie_10, spd.descripcion, COUNT(*) AS total
            FROM receta_diagnosticos rd
            JOIN recetas r ON r.id = rd.receta_id
            JOIN sofi_programa_diagnosticos spd
                ON upper(trim(regexp_replace(rd.codigo, '[^A-Za-z0-9.]', '', 'g'))) = upper(trim(spd.cie_10))
            WHERE r.created_at >= %s AND r.created_at < %s
            AND spd.programa = 'krystal'
            GROUP BY spd.cie_10, spd.descripcion
            ORDER BY total DESC
            LIMIT 15
        """, (fecha_inicio, fecha_fin))
        top_krystal = cur.fetchall()

        # ── Top 15 diagnosticos del programa VIVE_SANO_COMPLETO mas frecuentes ─
        cur.execute("""
            SELECT spd.cie_10, spd.descripcion, COUNT(*) AS total
            FROM receta_diagnosticos rd
            JOIN recetas r ON r.id = rd.receta_id
            JOIN sofi_programa_diagnosticos spd
                ON upper(trim(regexp_replace(rd.codigo, '[^A-Za-z0-9.]', '', 'g'))) = upper(trim(spd.cie_10))
            WHERE r.created_at >= %s AND r.created_at < %s
            AND spd.programa = 'vive_sano_completo'
            GROUP BY spd.cie_10, spd.descripcion
            ORDER BY total DESC
            LIMIT 15
        """, (fecha_inicio, fecha_fin))
        top_vive_sano = cur.fetchall()

    finally:
        cur.close()
        conn.close()
        connector.close()

    # ── Preparar datos para Chart.js ────────────────────────────
    clinicas_labels = json.dumps([html_lib.escape(c[0]) for c in top_clinicas], ensure_ascii=False)
    clinicas_data    = json.dumps([c[2] for c in top_clinicas])

    medicamentos_labels = json.dumps([html_lib.escape(m[0]) for m in top_medicamentos], ensure_ascii=False)
    medicamentos_data    = json.dumps([m[1] for m in top_medicamentos])

    principios_labels = json.dumps([html_lib.escape(p[0]) for p in top_principios], ensure_ascii=False)
    principios_data    = json.dumps([p[1] for p in top_principios])

    evolucion_labels = json.dumps([str(e[0]) for e in evolucion_diaria], ensure_ascii=False)
    evolucion_data    = json.dumps([e[1] for e in evolucion_diaria])

    petitorio_labels = json.dumps([p[0] for p in distribucion_petitorio], ensure_ascii=False)
    petitorio_data    = json.dumps([p[1] for p in distribucion_petitorio])

    motivos_labels = json.dumps([html_lib.escape(m[0]) for m in top_motivos_revision], ensure_ascii=False)
    motivos_data    = json.dumps([m[1] for m in top_motivos_revision])

    duraciones_labels = json.dumps([html_lib.escape(d[0]) for d in top_duraciones], ensure_ascii=False)
    duraciones_data    = json.dumps([d[1] for d in top_duraciones])

    no_petitorio_labels = json.dumps([html_lib.escape(m[0]) for m in top_no_petitorio], ensure_ascii=False)
    no_petitorio_data    = json.dumps([m[1] for m in top_no_petitorio])

    carta_garantia_labels = json.dumps([html_lib.escape(m[0]) for m in top_carta_garantia], ensure_ascii=False)
    carta_garantia_data    = json.dumps([m[1] for m in top_carta_garantia])

    pacientes_labels = json.dumps(
        [html_lib.escape(f"{p[0]} ({p[1]})") for p in top_pacientes], ensure_ascii=False
    )
    pacientes_data = json.dumps([p[2] for p in top_pacientes])

    # ── Filas de tabla de diagnosticos ───────────────────────────
    filas_diagnosticos = "".join(f"""
        <tr>
            <td>{html_lib.escape(d[0])}</td>
            <td>{html_lib.escape(d[1] or '-')} {'(PEAS)' if d[3] else '(NO PEAS)'}</td>
            <td class="num">{d[2]}</td>
        </tr>
    """ for d in top_diagnosticos)

    filas_krystal = "".join(f"""
        <tr>
            <td>{html_lib.escape(d[0])}</td>
            <td>{html_lib.escape(d[1] or '-')}</td>
            <td class="num">{d[2]}</td>
        </tr>
    """ for d in top_krystal)

    filas_vive_sano = "".join(f"""
        <tr>
            <td>{html_lib.escape(d[0])}</td>
            <td>{html_lib.escape(d[1] or '-')}</td>
            <td class="num">{d[2]}</td>
        </tr>
    """ for d in top_vive_sano)

    filas_clinicas = "".join(f"""
        <tr>
            <td>{html_lib.escape(c[0])}</td>
            <td>{html_lib.escape(c[1])}</td>
            <td class="num">{c[2]}</td>
            <td class="num">{c[3]}</td>
        </tr>
    """ for c in top_clinicas)

    html_out = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex">
<link rel="icon" type="image/x-icon" href="/assets/favicon_sanitas.ico">
<title>Dashboard Recetas — Sanitas</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {{
    --azul: #003b71;
    --azul-claro: #0072ce;
    --gris: #f4f6f8;
    --texto: #1f2937;
    --borde: #e2e8f0;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
    background: var(--gris);
    color: var(--texto);
    margin: 0;
    padding: 24px;
  }}
  h1 {{ color: var(--azul); margin-bottom: 4px; }}
  .subtitulo {{ color: #64748b; margin-top: 0; margin-bottom: 20px; }}
  form.filtros {{
    background: white;
    padding: 16px 20px;
    border-radius: 10px;
    border: 1px solid var(--borde);
    display: flex;
    gap: 14px;
    align-items: flex-end;
    flex-wrap: wrap;
    margin-bottom: 24px;
  }}
  form.filtros label {{ display: flex; flex-direction: column; font-size: 13px; color: #475569; gap: 4px; }}
  form.filtros input {{ padding: 8px 10px; border: 1px solid var(--borde); border-radius: 6px; font-size: 14px; }}
  form.filtros button {{
    background: var(--azul-claro); color: white; border: none;
    padding: 10px 18px; border-radius: 6px; cursor: pointer; font-weight: 600;
  }}
  .kpis {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 14px;
    margin-bottom: 24px;
  }}
  .kpi {{
    background: white; border: 1px solid var(--borde); border-radius: 10px;
    padding: 16px; text-align: center;
  }}
  .kpi .valor {{ font-size: 26px; font-weight: 700; color: var(--azul); }}
  .kpi .label {{ font-size: 12px; color: #64748b; margin-top: 4px; }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
    gap: 20px;
    margin-bottom: 24px;
  }}
  .card {{
    background: white; border: 1px solid var(--borde); border-radius: 10px;
    padding: 18px;
  }}
  .card h3 {{ margin-top: 0; color: var(--azul); font-size: 15px; }}
  .card.full {{ grid-column: 1 / -1; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 8px; background: var(--gris); color: #475569; }}
  td {{ padding: 8px; border-bottom: 1px solid var(--borde); }}
  td.num {{ text-align: right; }}

  #chartClinicas, #chartMedicamentos, #chartPrincipios, #chartDuraciones, #chartNoPetitorio, #chartPacientes {{ max-height: 480px; }}
  #chartEvolucion, #chartPetitorio {{ max-height: 340px; }}
</style>
</head>
<body>
  <h1>📊 Dashboard de Recetas — Sanitas Perú</h1>
  <p class="subtitulo">Del {fecha_inicio_str} al {fecha_fin_str}</p>

  <form class="filtros" method="GET">
    <input type="hidden" name="key" value="{html_lib.escape(key)}">
    <label>Desde
      <input type="date" name="fecha_inicio" value="{fecha_inicio_str}">
    </label>
    <label>Hasta
      <input type="date" name="fecha_fin" value="{fecha_fin_str}">
    </label>
    <button type="submit">Filtrar</button>
  </form>

  <div class="kpis">
    <div class="kpi"><div class="valor">{total_recetas}</div><div class="label">Recetas recibidas</div></div>
    <div class="kpi"><div class="valor">{total_revision_humana} ({pct_revision_humana}%)</div><div class="label">Requirió atención humana</div></div>
    <div class="kpi"><div class="valor">{total_por_ocr_bajo} ({pct_por_ocr_bajo}%)</div><div class="label">↳ Por manuscrito / OCR bajo</div></div>
    <div class="kpi"><div class="valor">{total_por_motivo_ia} ({pct_por_motivo_ia}%)</div><div class="label">↳ Por motivo detectado por IA</div></div>
    <div class="kpi"><div class="valor">{round(promedio_confianza * 100, 1)}%</div><div class="label">Confianza OCR promedio</div></div>
    <div class="kpi"><div class="valor">{total_meds}</div><div class="label">Total medicamentos recetados</div></div>
    <div class="kpi"><div class="valor">{con_carta}</div><div class="label">Con carta de garantía</div></div>
    <div class="kpi"><div class="valor">{pct_carta}%</div><div class="label">% con carta de garantía</div></div>
  </div>

  <div class="grid">
    <div class="card full">
      <h3>Recetas recibidas por día</h3>
      <canvas id="chartEvolucion"></canvas>
    </div>

    <div class="card">
      <h3>Top 15 clínicas por volumen de recetas</h3>
      <canvas id="chartClinicas"></canvas>
    </div>

    <div class="card">
      <h3>Medicamentos: en Petitorio vs. fuera de Petitorio</h3>
      <canvas id="chartPetitorio"></canvas>
    </div>

    <div class="card">
      <h3>Top 15 principios activos</h3>
      <canvas id="chartPrincipios"></canvas>
    </div>

    <div class="card">
      <h3>Top 15 duraciones de tratamiento</h3>
      <canvas id="chartDuraciones"></canvas>
    </div>

    <div class="card">
      <h3>Top 15 medicamentos solicitados fuera de Petitorio</h3>
      <canvas id="chartNoPetitorio"></canvas>
    </div>

    <div class="card">
        <h3>Top 15 medicamentos que requieren carta de garantía</h3>
        <canvas id="chartCartaGarantia"></canvas>
    </div>

    <div class="card">
      <h3>Top 15 pacientes con más solicitudes</h3>
      <canvas id="chartPacientes"></canvas>
    </div>

    <div class="card full">
      <h3>Top 15 diagnósticos más frecuentes</h3>
      <table>
        <thead><tr><th>Código</th><th>Descripción</th><th style="text-align:right">Total</th></tr></thead>
        <tbody>{filas_diagnosticos or '<tr><td colspan="3">Sin datos en el rango seleccionado</td></tr>'}</tbody>
      </table>
    </div>

    <div class="card">
        <h3>Top 15 diagnósticos — Programa Krystal</h3>
        <table>
            <thead><tr><th>Código</th><th>Descripción</th><th style="text-align:right">Total</th></tr></thead>
            <tbody>{filas_krystal or '<tr><td colspan="3">Sin datos en el rango seleccionado</td></tr>'}</tbody>
        </table>
        </div>

        <div class="card">
        <h3>Top 15 diagnósticos — Programa Vive Sano</h3>
        <table>
            <thead><tr><th>Código</th><th>Descripción</th><th style="text-align:right">Total</th></tr></thead>
            <tbody>{filas_vive_sano or '<tr><td colspan="3">Sin datos en el rango seleccionado</td></tr>'}</tbody>
        </table>
    </div>

    <div class="card full">
      <h3>Motivos de revisión detectados por IA (top 8)</h3>
      <canvas id="chartMotivos"></canvas>
    </div>

    <div class="card full">
      <h3>Detalle de clínicas (top 15)</h3>
      <table>
        <thead><tr><th>Clínica</th><th>RUC</th><th style="text-align:right">Recetas</th><th style="text-align:right">Rev. humana</th></tr></thead>
        <tbody>{filas_clinicas or '<tr><td colspan="4">Sin datos en el rango seleccionado</td></tr>'}</tbody>
      </table>
    </div>
  </div>

<script>
  const colorAzul = '#0072ce';

  Chart.defaults.plugins.tooltip.callbacks.label = function(context) {{
    const dataset = context.dataset;
    const total = dataset.data.reduce((acc, v) => acc + (typeof v === 'number' ? v : 0), 0);

    const indexAxis = context.chart.options.indexAxis || 'x';
    let valor = context.parsed;
    if (valor !== null && typeof valor === 'object') {{
        valor = indexAxis === 'y' ? valor.x : valor.y;
    }}

    const porcentaje = total > 0 ? ((valor / total) * 100).toFixed(1) : 0;
    const etiqueta = context.label || dataset.label || '';

    return `${{etiqueta}}: ${{valor}} (${{porcentaje}}%)`;
    }};
  
  new Chart(document.getElementById('chartEvolucion'), {{
    type: 'line',
    data: {{ labels: {evolucion_labels}, datasets: [{{ label: 'Recetas', data: {evolucion_data}, borderColor: colorAzul, backgroundColor: 'rgba(0,114,206,0.15)', fill: true, tension: 0.3 }}] }},
    options: {{ plugins: {{ legend: {{ display: false }} }} }}
  }});

  new Chart(document.getElementById('chartClinicas'), {{
    type: 'bar',
    data: {{ labels: {clinicas_labels}, datasets: [{{ label: 'Recetas', data: {clinicas_data}, backgroundColor: colorAzul }}] }},
    options: {{
      indexAxis: 'y',
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        y: {{ ticks: {{ autoSkip: false, font: {{ size: 10 }} }} }},
        x: {{ ticks: {{ font: {{ size: 10 }} }} }}
      }}
    }}
  }});

  const paletaPetitorio = {{
    'En petitorio': '#16a34a',
    'En petitorio (con carta)': '#f59e0b',
    'Fuera de petitorio': '#dc2626'
  }};
  const petitorioLabels = {petitorio_labels};
  new Chart(document.getElementById('chartPetitorio'), {{
    type: 'doughnut',
    data: {{
      labels: petitorioLabels,
      datasets: [{{ data: {petitorio_data}, backgroundColor: petitorioLabels.map(l => paletaPetitorio[l] || '#94a3b8') }}]
    }}
  }});

  new Chart(document.getElementById('chartMedicamentos'), {{
    type: 'bar',
    data: {{ labels: {medicamentos_labels}, datasets: [{{ label: 'Menciones', data: {medicamentos_data}, backgroundColor: '#0ea5e9' }}] }},
    options: {{
      indexAxis: 'y',
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        y: {{ ticks: {{ autoSkip: false, font: {{ size: 10 }} }} }},
        x: {{ ticks: {{ font: {{ size: 10 }} }} }}
      }}
    }}
  }});

  new Chart(document.getElementById('chartPrincipios'), {{
    type: 'bar',
    data: {{ labels: {principios_labels}, datasets: [{{ label: 'Menciones', data: {principios_data}, backgroundColor: '#7c3aed' }}] }},
    options: {{
      indexAxis: 'y',
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        y: {{ ticks: {{ autoSkip: false, font: {{ size: 10 }} }} }},
        x: {{ ticks: {{ font: {{ size: 10 }} }} }}
      }}
    }}
  }});

  new Chart(document.getElementById('chartMotivos'), {{
    type: 'bar',
    data: {{ labels: {motivos_labels}, datasets: [{{ label: 'Recetas', data: {motivos_data}, backgroundColor: '#f97316' }}] }},
    options: {{
      indexAxis: 'y',
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        y: {{ ticks: {{ autoSkip: false, font: {{ size: 10 }} }} }},
        x: {{ ticks: {{ font: {{ size: 10 }} }} }}
      }}
    }}
  }});

  new Chart(document.getElementById('chartDuraciones'), {{
    type: 'bar',
    data: {{ labels: {duraciones_labels}, datasets: [{{ label: 'Menciones', data: {duraciones_data}, backgroundColor: '#059669' }}] }},
    options: {{
      indexAxis: 'y',
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        y: {{ ticks: {{ autoSkip: false, font: {{ size: 10 }} }} }},
        x: {{ ticks: {{ font: {{ size: 10 }} }} }}
      }}
    }}
  }});

  new Chart(document.getElementById('chartNoPetitorio'), {{
    type: 'bar',
    data: {{ labels: {no_petitorio_labels}, datasets: [{{ label: 'Menciones', data: {no_petitorio_data}, backgroundColor: '#dc2626' }}] }},
    options: {{
      indexAxis: 'y',
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        y: {{ ticks: {{ autoSkip: false, font: {{ size: 10 }} }} }},
        x: {{ ticks: {{ font: {{ size: 10 }} }} }}
      }}
    }}
  }});

  new Chart(document.getElementById('chartPacientes'), {{
    type: 'bar',
    data: {{ labels: {pacientes_labels}, datasets: [{{ label: 'Recetas', data: {pacientes_data}, backgroundColor: '#4338ca' }}] }},
    options: {{
      indexAxis: 'y',
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        y: {{ ticks: {{ autoSkip: false, font: {{ size: 10 }} }} }},
        x: {{ ticks: {{ font: {{ size: 10 }} }} }}
      }}
    }}
  }});

  new Chart(document.getElementById('chartCartaGarantia'), {{
        type: 'bar',
        data: {{ labels: {carta_garantia_labels}, datasets: [{{ label: 'Menciones', data: {carta_garantia_data}, backgroundColor: '#f59e0b' }}] }},
        options: {{
        indexAxis: 'y',
        plugins: {{ legend: {{ display: false }} }},
        scales: {{
            y: {{ ticks: {{ autoSkip: false, font: {{ size: 10 }} }} }},
            x: {{ ticks: {{ font: {{ size: 10 }} }} }}
        }}
        }}
    }});
</script>
</body>
</html>"""

    return (html_out, 200, HEADERS_HTML)


# ──────────────────────────────────────────────────────────────
# PUNTO DE ENTRADA
# Input JSON: { "celular", "ruta", "doi", "chat_id" }
# ──────────────────────────────────────────────────────────────
@functions_framework.http
def procesar_receta(request):

    if request.method == "OPTIONS":
        return ("", 204, HEADERS)

    # ── Ruteo — dashboard de estadisticas de recetas ──────────
    if request.path.rstrip("/") == "/dashboard-recetas":
        return handle_dashboard_recetas(request)
    if request.path.rstrip("/") == "/reporte-sms-programas":
        return handle_reporte_sms_programas(request)
    if request.path.rstrip("/") == "/enviar-sms-programas":
        return handle_enviar_sms_programas(request)

    if not validar_api_key(request):
        return (json.dumps({"error": "API Key invalida o ausente"}), 401, HEADERS)

    try:
        data = request.get_json(force=True, silent=True) or {}
        if not data:
            return (json.dumps({"error": "No se recibio payload JSON"}), 400, HEADERS)

        celular = data.get("celular", "").strip()
        ruta    = data.get("ruta", "").strip()
        doi     = data.get("doi", "").strip()
        chat_id = data.get("chat_id", "").strip()

        if not ruta:
            return (json.dumps({"error": "El campo 'ruta' es obligatorio"}), 400, HEADERS)
        if not celular:
            return (json.dumps({"error": "El campo 'celular' es obligatorio"}), 400, HEADERS)

        print(f"[INFO] Receta recibida — celular={celular} doi={doi} chat_id={chat_id}")

        # ── PASO 1: Detectar tipo de archivo ──────────────────
        mime_type   = detectar_mime_type(ruta)
        es_pdf_flag = mime_type == MIME_PDF
        print(f"[INFO] MIME detectado: {mime_type} | PDF: {es_pdf_flag}")

        # ── PASO 2: Descargar archivo ─────────────────────────
        archivo_bytes = descargar_archivo(ruta)

        # ── PASO 3: Guardar en Cloud Storage ──────────────────
        ext            = "pdf" if es_pdf_flag else "jpg"
        now_lima = datetime.now(LIMA_TZ)
        archivo_nombre = f"recetas-entrantes/{celular}_{now_lima.strftime('%Y%m%d_%H%M%S')}.{ext}"
        guardar_en_storage(archivo_bytes, archivo_nombre, mime_type)

        # ── PASO 4: Document AI — OCR + nivel de confianza ────
        texto_extraido, confianza_ocr, tiene_manuscrito = procesar_con_document_ai(
            archivo_bytes, mime_type
        )
        print(f"[INFO] OCR — confianza: {confianza_ocr} | manuscrito: {tiene_manuscrito}")
        print(f"[INFO] Texto extraido (200 chars): {texto_extraido[:200]}")

        # ── PASO 5: Gemini — analizar y estructurar receta ────
        # Se ejecuta SIEMPRE, incluso si es manuscrito, para rescatar
        # los campos que sí sean legibles (cabecera, datos de la clinica,
        # etc.) aunque el detalle de medicamentos requiera revision humana.
        analisis = analizar_con_gemini(texto_extraido)
        print(f"[INFO] Estado cobertura: {analisis.get('estado_cobertura')}")

        analisis["confianza_ocr"]    = confianza_ocr
        analisis["tiene_manuscrito"] = tiene_manuscrito

        if tiene_manuscrito:
            # El manuscrito siempre fuerza revision humana,
            # sin importar lo que haya determinado Gemini para el resto.
            analisis["atencion_humana"]        = True
            analisis["motivo_atencion_humana"] = "TEXTO MANUSCRITO O CONFIANZA OCR BAJA (< 90%)"
        elif analisis.get("requiere_revision", False):
            analisis["atencion_humana"]        = True
            analisis["motivo_atencion_humana"] = analisis.get("motivo_revision", "").upper()
        else:
            analisis["atencion_humana"]        = False
            analisis["motivo_atencion_humana"] = ""

        # ── PASO 6: Guardar en base de datos ──────────────────
        if tiene_manuscrito:
            receta_id, tiene_programa = guardar_receta_manuscrita(
                celular, doi, chat_id, archivo_nombre, analisis, confianza_ocr
            )
            print("[INFO] Manuscrito detectado — derivando a atencion humana")
            return (json.dumps({
                "status":                 "revision_humana",
                "receta_id":              receta_id,
                "confianza_ocr":          confianza_ocr,
                "tiene_manuscrito":       True,
                "atencion_humana":        True,
                "motivo_atencion_humana": analisis["motivo_atencion_humana"],
                "para_programas":         MENSAJE_PROGRAMAS if tiene_programa else MENSAJE_SIN_PROGRAMAS,
                "datos_extraidos": {
                    "paciente":       analisis.get("nombre_paciente"),
                    "dni":            analisis.get("dni_paciente"),
                    "medico":         analisis.get("nombre_medico"),
                    "cmp":            analisis.get("cmp_medico"),
                    "fecha_emision":  analisis.get("fecha_emision"),
                    "aseguradora":    analisis.get("aseguradora"),
                    "ruc_clinica":    analisis.get("ruc_clinica"),
                    "nombre_clinica": analisis.get("nombre_clinica"),
                },
                "respuesta_paciente": (
                    #f"⏳ Tu receta #{receta_id} esta siendo revisada por un asesor. "
                    f"✅ Hemos recibido correctamente tu receta #{receta_id}"
                    #f"Te contactaremos en los proximos minutos."
                )
            }, ensure_ascii=False), 200, HEADERS)

        receta_id, tiene_programa = guardar_en_base_datos(
            celular, doi, chat_id, archivo_nombre, analisis, confianza_ocr
        )

        # ── PASO 7: Construir salida ───────────────────────────
        resumen            = construir_resumen(analisis)
        respuesta_paciente = generar_respuesta_paciente(analisis, receta_id)

        return (json.dumps({
            "status":                 "ok",
            "receta_id":              receta_id,
            "confianza_ocr":          confianza_ocr,
            "tiene_manuscrito":       tiene_manuscrito,
            "es_pdf":                 es_pdf_flag,
            "atencion_humana":        analisis["atencion_humana"],
            "motivo_atencion_humana": analisis["motivo_atencion_humana"],
            "resumen_receta":         resumen,
            "para_programas":         MENSAJE_PROGRAMAS if tiene_programa else MENSAJE_SIN_PROGRAMAS,
            "datos_extraidos": {
                "paciente":       analisis.get("nombre_paciente"),
                "dni":            analisis.get("dni_paciente"),
                "medico":         analisis.get("nombre_medico"),
                "cmp":            analisis.get("cmp_medico"),
                "fecha_emision":  analisis.get("fecha_emision"),
                "aseguradora":    analisis.get("aseguradora"),
                "ruc_clinica":    analisis.get("ruc_clinica"),
                "nombre_clinica": analisis.get("nombre_clinica"),
                "diagnosticos":   analisis.get("diagnosticos", []),
                "medicamentos":   analisis.get("medicamentos", [])
            },
            "estado_cobertura":   analisis.get("estado_cobertura"),
            "respuesta_paciente": respuesta_paciente
        }, ensure_ascii=False), 200, HEADERS)

    except Exception as e:
        print(f"[ERROR] {str(e)}")
        return (json.dumps({"error": str(e)}), 500, HEADERS)

# ──────────────────────────────────────────────────────────────
# PASO 2: DESCARGAR ARCHIVO
# ──────────────────────────────────────────────────────────────
def descargar_archivo(url: str) -> bytes:
    response = requests.get(url, timeout=60)  # 60s para PDFs grandes
    response.raise_for_status()
    return response.content


# ──────────────────────────────────────────────────────────────
# PASO 3: GUARDAR EN CLOUD STORAGE
# ──────────────────────────────────────────────────────────────
def guardar_en_storage(contenido: bytes, nombre_archivo: str, mime_type: str):
    cliente = storage.Client()
    bucket  = cliente.bucket(BUCKET_NAME)
    blob    = bucket.blob(nombre_archivo)
    blob.upload_from_string(contenido, content_type=mime_type)
    print(f"[INFO] Guardado en gs://{BUCKET_NAME}/{nombre_archivo}")


# ──────────────────────────────────────────────────────────────
# PASO 4: DOCUMENT AI — OCR + CONFIANZA
# ──────────────────────────────────────────────────────────────
def procesar_con_document_ai(archivo_bytes: bytes, mime_type: str):
    """
    Extrae texto del archivo (imagen o PDF).
    Calcula confianza promedio de los tokens.
    Si confianza < UMBRAL_CONFIANZA se asume manuscrito → atención humana.
    """
    cliente   = documentai.DocumentProcessorServiceClient()
    nombre    = cliente.processor_path(PROJECT_ID, LOCATION, PROCESSOR_ID)
    documento = documentai.RawDocument(content=archivo_bytes, mime_type=mime_type)
    req       = documentai.ProcessRequest(name=nombre, raw_document=documento)
    resultado = cliente.process_document(request=req)
    doc       = resultado.document

    confidences = []
    for page in doc.pages:
        for token in page.tokens:
            if hasattr(token, "layout") and hasattr(token.layout, "confidence"):
                confidences.append(token.layout.confidence)

    confianza_ocr    = round(sum(confidences) / len(confidences), 4) if confidences else 1.0
    tiene_manuscrito = confianza_ocr < UMBRAL_CONFIANZA

    print(f"[INFO] Confianza OCR: {confianza_ocr} | Manuscrito: {tiene_manuscrito}")
    return doc.text, confianza_ocr, tiene_manuscrito

# ──────────────────────────────────────────────────────────────
# PASO 5: GEMINI — ANÁLISIS Y ESTRUCTURACIÓN
# Modifica el prompt aquí para afinar la extracción
# ──────────────────────────────────────────────────────────────
def analizar_con_gemini(texto_receta: str, max_reintentos: int = 4) -> dict:
    cliente_genai = genai.Client(
        vertexai=True, project=PROJECT_ID, location="us"
    )

    grounding_tool = genai_types.Tool(google_search=genai_types.GoogleSearch())

    prompt = f"""Eres un sistema de analisis de recetas medicas para Sanitas Peru.
    Analiza el siguiente texto extraido de una receta medica y responde UNICAMENTE con un JSON valido.
    Sin markdown, sin explicaciones, solo el JSON.

    TEXTO DE LA RECETA:
    {texto_receta}

    Estructura de respuesta:
    {{
    "nombre_paciente":  "",
    "dni_paciente":     "",
    "nombre_medico":    "",
    "cmp_medico":       "",
    "fecha_emision":    "",
    "aseguradora":      "",
    "ruc_clinica":      "",
    "nombre_clinica":   "",
    "diagnosticos": [
        {{ "codigo": "", "descripcion": "" }}
    ],
    "medicamentos": [
        {{
        "nombre":            "",
        "principio_activo":  "",
        "dosis":             "",
        "frecuencia":        "",
        "duracion":          "",
        "cantidad":          "",
        "cobertura":         "cubierto|parcial|no_cubierto|revisar",
        "receta_extendida":  "S|N",
        "alerta":            ""
        }}
    ],
    "estado_cobertura":  "aprobado|revision|rechazado",
    "requiere_revision": false,
    "motivo_revision":   "",
    "confianza":         0.95
    }}

    Reglas:
    - Todo texto en MAYUSCULAS.
    - fecha_emision siempre en formato YYYY-MM-DD. Si aparece como "inicio de vigencia", usar ese valor.
    - Si la receta tiene mas de 30 dias, requiere_revision=true, motivo="RECETA VENCIDA".
    - Si hay medicamentos controlados, requiere_revision=true.
    - Si no se ve firma o sello del medico, requiere_revision=true.
    - cobertura="revisar" si no puedes determinarlo con certeza.
    - CRITICO — nombre del medicamento: palabras como "COMPRIMIDO", "COMPRIMIDOS", "TABLETA", "TABLETAS",
    "CAPSULA", "CAPSULAS", "JARABE", "SPRAY", "SPRAY NASAL", "SOLUCION", "GOTAS", "CREMA", "OVULO"
    describen unicamente la FORMA FARMACEUTICA (presentacion fisica), nunca son el nombre del medicamento.
    NUNCA uses solo una de estas palabras como valor de "nombre". Debes buscar en el texto completo de la
    receta el nombre real (comercial o generico) que acompaña a esa forma farmaceutica, aunque este en
    otra linea, antes o despues de la forma farmaceutica, o junto a la concentracion (mg/mcg).
    Si tras revisar todo el texto no logras identificar ningun nombre real asociado a esa forma
    farmaceutica, entonces usa como "nombre" la forma farmaceutica + la concentracion tal como aparecen
    (ej. "TABLETA 30 MG"), deja "principio_activo" vacio "", y marca requiere_revision=true con
    motivo_revision="MEDICAMENTO NO IDENTIFICABLE".
    - Para principio_activo: si ves la palabra DCI antes del nombre, usar ese valor.
    Si empieza con "ACIDO", omitir "ACIDO" (ej: "ACIDO ACETILSALICILICO" -> "ACETILSALICILICO").
    - CRITICO — principio_activo debe estar completo y bien escrito: verifica letra por letra
    que no omitiste ni recortaste caracteres al final o en medio de la palabra
    (ej. "ESOMEPRAZOL" no "ESOMEPRAZO", "AMLODIPINO" no "AMLODIPIN",
    "ATORVASTATINA" no "ATORVASTATIN"). Si tienes dudas sobre la ortografia exacta del
    DCI, usa tu conocimiento del vocabulario farmaceutico estandar para completarlo
    correctamente en vez de copiar una version truncada del texto de la receta.
    - principio_activo: SIEMPRE debe ser el nombre generico (DCI), nunca el nombre comercial/marca.
    Si el medicamento esta escrito solo con su nombre comercial, DEBES usar la herramienta de
    busqueda de Google para verificar cual es el principio activo real que corresponde a esa marca
    en el mercado farmaceutico peruano (DIGEMID, vademecum Peru), especialmente si es una marca
    local o poco conocida que no reconoces con certeza. NO adivines ni dejes el nombre comercial
    como principio_activo si tienes duda: busca antes de responder.
    Ejemplos reales de este tipo de caso (marca -> principio activo que debes encontrar):
        - "RINEXFLU" -> "FEXOFENADINA CLORHIDRATO"
        - "RINOKID" -> "SODIO CLORURO"
        - "DRONAVAL" -> "IBANDRONICO ACIDO"
    Si es una combinacion de dos o mas principios activos, sepáralos con "/"
    (ej. "MICARDIS AMLO 80MG/5MG" -> "TELMISARTAN/AMLODIPINO").
    Solo si, despues de buscar, no logras determinar el principio activo con certeza,
    deja el nombre comercial tal cual.
    - cantidad: calcular dosis_diaria x duracion_dias.
    - receta_extendida="S" si el tratamiento supera 30 dias, sino "N".
    - ruc_clinica: el RUC (11 digitos) de la clinica, IPRESS o centro medico que emite la receta.
    Generalmente aparece en el encabezado o pie de pagina del documento, junto a la razon social.
    Si no aparece ningun RUC en el texto, dejar el campo vacio "".
    - nombre_clinica: la razon social o nombre comercial de la clinica/centro medico que emite la receta
    (normalmente en el encabezado, junto al RUC o el logo). Si no se identifica con certeza, dejar vacio ""."""

    respuesta = None
    for intento in range(max_reintentos):
        try:
            respuesta = cliente_genai.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    tools=[grounding_tool],
                    temperature=1.0,  # Google recomienda 1.0 para grounding con Search
                ),
            )
            break
        except (ClientError, ServerError) as e:
            es_429 = getattr(e, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(e)
            if not es_429 or intento == max_reintentos - 1:
                print(f"[ERROR] Vertex AI Gemini — reintentos agotados o error no recuperable: {e}")
                raise
            espera = (2 ** intento) + random.uniform(0, 1)
            print(f"[WARN] 429 Vertex AI — reintentando en {espera:.1f}s (intento {intento + 1}/{max_reintentos})")
            time.sleep(espera)

    texto_respuesta = respuesta.text.strip()

    if texto_respuesta.startswith("```"):
        texto_respuesta = texto_respuesta.split("```")[1]
        if texto_respuesta.startswith("json"):
            texto_respuesta = texto_respuesta[4:]

    return json.loads(texto_respuesta.strip())

# ──────────────────────────────────────────────────────────────
# PASO 6A: GUARDAR RECETA MANUSCRITA (sin análisis Gemini)
# ──────────────────────────────────────────────────────────────
def guardar_receta_manuscrita(
    celular: str, doi: str, chat_id: str,
    archivo_url: str, analisis: dict, confianza_ocr: float
) -> int:
    now_lima = datetime.now(LIMA_TZ).replace(tzinfo=None)
    conn, connector = get_db_connection()
    cur             = conn.cursor()
    try:
        receta_id = _insertar_receta(
            cur,
            celular=celular, doi=doi, chat_id=chat_id, archivo_url=archivo_url,
            estado="revision_humana",
            confianza_ocr=confianza_ocr,
            requiere_revision=True,
            motivo_revision=analisis.get("motivo_atencion_humana", "TEXTO MANUSCRITO O CONFIANZA OCR BAJA"),
            now_lima=now_lima,
            analisis=analisis,
        )

        # Diagnosticos/medicamentos que Gemini sí logró leer, como referencia
        for diag in analisis.get("diagnosticos", []):
            cur.execute("""
                INSERT INTO receta_diagnosticos (receta_id, codigo, descripcion, created_at)
                VALUES (%s,%s,%s,%s)
            """, (receta_id, diag.get("codigo"), diag.get("descripcion"), now_lima))

        for med in analisis.get("medicamentos", []):
            cur.execute("""
                INSERT INTO receta_medicamentos (
                    receta_id, nombre_medicamento, principio_activo,
                    dosis, frecuencia, duracion, cantidad,
                    cobertura, receta_extendida, en_petitorio, requiere_carta_garantia,
                    requiere_cuida, alerta,
                    created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                receta_id, med.get("nombre"), med.get("principio_activo"),
                med.get("dosis"), med.get("frecuencia"), med.get("duracion"),
                med.get("cantidad"), med.get("cobertura"),
                med.get("receta_extendida", "N"), med.get("en_petitorio", True),
                med.get("requiere_carta_garantia", False),
                med.get("requiere_cuida", False),
                med.get("alerta"), now_lima
            ))

        cur.execute("""
            INSERT INTO receta_eventos (receta_id, evento, detalle, created_at)
            VALUES (%s,%s,%s,%s)
        """, (
            receta_id, "recibida",
            f"Manuscrito detectado. Confianza OCR: {confianza_ocr}",
            now_lima
        ))

        programas = obtener_programas_receta(cur, analisis.get("diagnosticos", []))
        if programas:
            guardar_pacientes_programas(cur, receta_id, celular, analisis, programas, now_lima)

        conn.commit()
        return receta_id, bool(programas)

    except Exception as e:
        conn.rollback()
        print(f"[ERROR DB manuscrita] {str(e)}")
        raise
    finally:
        cur.close()
        conn.close()
        connector.close()


# ──────────────────────────────────────────────────────────────
# PASO 6B: GUARDAR RECETA PROCESADA CON GEMINI
# ──────────────────────────────────────────────────────────────
def guardar_en_base_datos(
    celular: str, doi: str, chat_id: str,
    archivo_url: str, analisis: dict, confianza_ocr: float
) -> int:
    now_lima = datetime.now(LIMA_TZ).replace(tzinfo=None)
    conn, connector = get_db_connection()
    cur             = conn.cursor()

    try:
        receta_id = _insertar_receta(
            cur,
            celular=celular, doi=doi, chat_id=chat_id, archivo_url=archivo_url,
            estado=analisis.get("estado_cobertura", "pendiente"),
            confianza_ocr=confianza_ocr,
            requiere_revision=analisis.get("requiere_revision", True),
            motivo_revision=analisis.get("motivo_atencion_humana"),
            now_lima=now_lima,
            analisis=analisis,
        )

        for diag in analisis.get("diagnosticos", []):
            cur.execute("""
                INSERT INTO receta_diagnosticos (receta_id, codigo, descripcion, created_at)
                VALUES (%s,%s,%s,%s)
            """, (receta_id, diag.get("codigo"), diag.get("descripcion"), now_lima))

        for med in analisis.get("medicamentos", []):
            # Si no se identificó el principio activo, usar el nombre del medicamento como respaldo
            valor_busqueda = med.get("principio_activo") or med.get("nombre", "")

            detalle_petitorio     = verificar_petitorio_detalle(cur, valor_busqueda)
            med["petitorio_detalle"]        = detalle_petitorio
            med["en_petitorio"]             = all(d["en_petitorio"] for d in detalle_petitorio) if detalle_petitorio else False
            med["requiere_carta_garantia"]  = any(d["requiere_carta_garantia"] for d in detalle_petitorio) if detalle_petitorio else False
            med["requiere_cuida"]           = any(d["requiere_cuida"] for d in detalle_petitorio) if detalle_petitorio else False
        
            cur.execute("""
                INSERT INTO receta_medicamentos (
                    receta_id, nombre_medicamento, principio_activo,
                    dosis, frecuencia, duracion, cantidad,
                    cobertura, receta_extendida, en_petitorio, requiere_carta_garantia,
                    requiere_cuida, alerta,
                    created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                receta_id, med.get("nombre"), med.get("principio_activo"),
                med.get("dosis"), med.get("frecuencia"), med.get("duracion"),
                med.get("cantidad"), med.get("cobertura"),
                med.get("receta_extendida", "N"), med.get("en_petitorio", True),
                med.get("requiere_carta_garantia", False),
                med.get("requiere_cuida", False),
                med.get("alerta"), now_lima
            ))

        cur.execute("""
            INSERT INTO receta_eventos (receta_id, evento, detalle, created_at)
            VALUES (%s,%s,%s,%s)
        """, (
            receta_id, "recibida",
            f"Confianza OCR: {confianza_ocr} | Estado: {analisis.get('estado_cobertura')}",
            now_lima
        ))

        programas = obtener_programas_receta(cur, analisis.get("diagnosticos", []))
        if programas:
            guardar_pacientes_programas(cur, receta_id, celular, analisis, programas, now_lima)

        conn.commit()
        return receta_id, bool(programas)

    except Exception as e:
        conn.rollback()
        print(f"[ERROR DB] {str(e)}")
        raise
    finally:
        cur.close()
        conn.close()
        connector.close()


# ──────────────────────────────────────────────────────────────
# PASO 7A: RESUMEN PARA EL ASESOR
# ──────────────────────────────────────────────────────────────
def construir_resumen(analisis: dict) -> dict:
    meds        = analisis.get("medicamentos", [])
    lineas_meds = []

    for m in meds:
        emoji = {
            "cubierto":    "✅",
            "parcial":     "⚠️",
            "no_cubierto": "❌",
            "revisar":     "🔍"
        }.get(m.get("cobertura", "revisar"), "🔍")

        linea = (
            f"{emoji} {m.get('nombre', 'N/A')} — "
            f"{m.get('dosis', '')} {m.get('frecuencia', '')} "
            f"por {m.get('duracion', 'N/A')}"
        )
        if m.get("alerta"):
            linea += f" ⚠️ {m['alerta']}"
        lineas_meds.append(linea)

    diagnosticos = analisis.get("diagnosticos", [])
    lineas_diag  = [
        f"{d.get('codigo', '')} - {d.get('descripcion', '')}" for d in diagnosticos
    ]

    manuscrito_texto = "⚠️ SI — requiere validacion humana" if analisis.get("tiene_manuscrito") else "✅ No"

    return {
        "paciente":        f"{analisis.get('nombre_paciente', 'N/A')} (DNI: {analisis.get('dni_paciente', 'N/A')})",
        "medico":          f"{analisis.get('nombre_medico', 'N/A')} (CMP: {analisis.get('cmp_medico', 'N/A')})",
        "aseguradora":     analisis.get("aseguradora", "N/A"),
        "fecha_emision":   analisis.get("fecha_emision", "N/A"),
        "diagnosticos":    lineas_diag,
        "medicamentos":    lineas_meds,
        "cobertura":       analisis.get("estado_cobertura", "N/A").upper(),
        "confianza_ocr":   f"{round(analisis.get('confianza_ocr', 0) * 100, 1)}%",
        "manuscrito":      manuscrito_texto,
        "atencion_humana": "🚨 SI" if analisis.get("atencion_humana") else "🤖 Automatico",
        "motivo":          analisis.get("motivo_atencion_humana", "")
    }


# ──────────────────────────────────────────────────────────────
# PASO 7B: RESPUESTA PARA EL PACIENTE (mensaje WhatsApp)
# ──────────────────────────────────────────────────────────────
def generar_respuesta_paciente(analisis: dict, receta_id: int) -> str:
    estado = analisis.get("estado_cobertura", "")
    meds   = analisis.get("medicamentos", [])
    diags  = analisis.get("diagnosticos", [])

    # Construir líneas de diagnósticos
    lineas_diag = ""
    for d in diags:
        codigo = d.get("codigo", "")
        desc   = d.get("descripcion", "")
        if codigo or desc:
            lineas_diag += f"  • {codigo} - {desc}\n"

    # Construir líneas de medicamentos
    lineas_meds = ""
    for m in meds:
        nombre = m.get("nombre", "")
        dosis  = m.get("dosis", "")
        freq   = m.get("frecuencia", "")
        dur    = m.get("duracion", "")
        emoji  = {
            "cubierto":    "✅",
            "parcial":     "⚠️",
            "no_cubierto": "❌",
            "revisar":     "🔍"
        }.get(m.get("cobertura", "revisar"), "🔍")
        if nombre:
            detalle = m.get("petitorio_detalle", [])
            if len(detalle) > 1:
                partes = []
                for d in detalle:
                    if d.get("requiere_cuida"):
                        marca = "📝 CG CUID"
                    elif d["en_petitorio"] and d.get("requiere_carta_garantia"):
                        marca = "📝 Carta de Garantía"
                    elif d["en_petitorio"]:
                        marca = "✅"
                    else:
                        marca = "❌"
                    partes.append(f"{d['componente']} {marca}")
                marca_petitorio = f"({' / '.join(partes)})"
            elif len(detalle) == 1:
                d = detalle[0]
                if d.get("requiere_cuida"):
                    marca_petitorio = "(📝 CG CUID)"
                elif d["en_petitorio"] and d.get("requiere_carta_garantia"):
                    marca_petitorio = "(📝 Carta de Garantía)"
                elif d["en_petitorio"]:
                    marca_petitorio = "(Petitorio ✅)"
                else:
                    marca_petitorio = "(Petitorio ❌)"
            else:
                marca_petitorio = ""

            lineas_meds += f"  {emoji} {nombre} {marca_petitorio}\n"
            if dosis or freq:
                lineas_meds += f"     {dosis} {freq}\n"
            if dur:
                lineas_meds += f"     Duración: {dur}\n"

    # Construir mensaje según estado
    if analisis.get("atencion_humana"):
        mensaje = (
        f"⏳ Receta #{receta_id} en revisión\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {analisis.get('nombre_paciente', 'N/A')}\n"
        f"🩺 Dr. {analisis.get('nombre_medico', 'N/A')}\n"
        f"📅 Emisión: {analisis.get('fecha_emision', 'N/A')}\n"
        "Un asesor revisará tu receta y te contactará en los próximos minutos.\n"
        )

        if lineas_diag:
            mensaje += f"\n📋 Diagnóstico:\n{lineas_diag}"

        if lineas_meds:
            mensaje += f"\n💊 Medicamentos:\n{lineas_meds}"

        return mensaje
    elif estado == "aprobado":
        msg  = f"✅ Receta #{receta_id} recibida correctamente\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"👤 {analisis.get('nombre_paciente', 'N/A')}\n"
        msg += f"🩺 Dr. {analisis.get('nombre_medico', 'N/A')}\n"
        msg += f"📅 Emisión: {analisis.get('fecha_emision', 'N/A')}\n"
        if lineas_diag:
            msg += f"\n📋 Diagnóstico:\n{lineas_diag}"
        if lineas_meds:
            msg += f"\n💊 Medicamentos:\n{lineas_meds}"
        #msg += f"\nEn breve recibirás la confirmación de tu pedido."
        return msg
    else:
        msg  = f"✅ Receta #{receta_id} recibida correctamente\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"👤 {analisis.get('nombre_paciente', 'N/A')}\n"
        msg += f"🩺 Dr. {analisis.get('nombre_medico', 'N/A')}\n"
        if lineas_meds:
            msg += f"\n💊 Medicamentos:\n{lineas_meds}"
        #msg += f"\nUn asesor se comunicará contigo pronto."
        return msg

# ──────────────────────────────────────────────────────────────
# INSERT BASE — usado por las dos rutas de guardado
# ──────────────────────────────────────────────────────────────
def _insertar_receta(
    cur, *, celular, doi, chat_id, archivo_url, estado,
    confianza_ocr, requiere_revision, motivo_revision, now_lima, analisis=None
) -> int:
    analisis = analisis or {}
    cur.execute("""
        INSERT INTO recetas (
            celular, doi, chat_id, canal,
            nombre_paciente, dni_paciente,
            nombre_medico, cmp_medico, fecha_emision,
            aseguradora, ruc_clinica, nombre_clinica,
            archivo_url, estado,
            confianza_ocr, requiere_revision, motivo_revision,
            fecha_recepcion, created_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
    """, (
        celular, doi, chat_id, "whatsapp",
        analisis.get("nombre_paciente"),
        analisis.get("dni_paciente"),
        analisis.get("nombre_medico"),
        analisis.get("cmp_medico"),
        analisis.get("fecha_emision") or None,
        analisis.get("aseguradora"),
        analisis.get("ruc_clinica") or None,
        analisis.get("nombre_clinica") or None,
        archivo_url, estado,
        confianza_ocr, requiere_revision, motivo_revision,
        now_lima, now_lima
    ))
    return cur.fetchone()[0]

# ──────────────────────────────────────────────────────────────
# SMS PROGRAMAS DE SALUD — envío vía Danaconnect + registro
# ──────────────────────────────────────────────────────────────
DANACONNECT_URL_PROGRAMAS  = os.environ.get("DANACONNECT_URL")
DANACONNECT_AUTH_PROGRAMAS = os.environ.get("DANACONNECT_AUTH")

def _limpiar_nombre_sms(nombre):
    return re.sub(
        r'[\U0001F300-\U0001F9FF\U00002600-\U000026FF'
        r'\U00002700-\U000027BF\U0001F1E6-\U0001F1FF]',
        '', nombre or '', flags=re.UNICODE
    )

def enviar_sms_danaconnect_programa(numero, mensaje, link):
    payload = {"NUM": f" {numero}", "TEXT": mensaje, "URL": link}
    headers = {
        "Content-Type":  "application/json",
        "Authorization": DANACONNECT_AUTH_PROGRAMAS
    }
    response = requests.post(DANACONNECT_URL_PROGRAMAS, json=payload, headers=headers, timeout=30)
    response.raise_for_status()


def _enviar_sms_a_pacientes(ids: list = None) -> dict:
    base_url = os.environ.get("BASE_URL", "https://sntspe.com")
    campana = f"programas_salud_{datetime.now(LIMA_TZ).strftime('%Y%m')}"

    conn, connector = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT mensaje_template FROM programa_sms_config ORDER BY id DESC LIMIT 1")
        fila_config = cur.fetchone()
        mensaje_template = fila_config[0] if fila_config else ""

        if not mensaje_template.strip():
            return {"enviados": 0, "omitidos": 0, "errores": 0, "error": "Falta configurar el mensaje en programa_sms_config"}

        if ids:
            cur.execute("""
                SELECT pp.id, pp.celular, pp.nombre_paciente, pp.programa
                FROM pacientes_programas pp
                WHERE pp.id = ANY(%s)
                  AND NOT EXISTS (
                      SELECT 1 FROM programa_sms_envios se WHERE se.paciente_programa_id = pp.id
                  )
            """, (ids,))
            total_solicitados = len(ids)
        else:
            cur.execute("""
                SELECT pp.id, pp.celular, pp.nombre_paciente, pp.programa
                FROM pacientes_programas pp
                WHERE NOT EXISTS (
                    SELECT 1 FROM programa_sms_envios se WHERE se.paciente_programa_id = pp.id
                )
            """)
            total_solicitados = None

        candidatos = cur.fetchall()
        omitidos = (total_solicitados - len(candidatos)) if total_solicitados is not None else 0

        enviados = 0
        errores = 0

        # ── Conexión separada a sofi_whatsapp para registrar el tracking ──
        conn_sw, connector_sw = get_db_connection_sofi_whatsapp()
        cur_sw = conn_sw.cursor()

        for paciente_id, celular, nombre, programa in candidatos:
            nombre_limpio  = _limpiar_nombre_sms(nombre)
            mensaje_final  = mensaje_template.replace("{Nombre}", nombre_limpio or "")

            code = _generar_code_click_programa(celular)
            link_corto = f"{base_url}/ps/{code}"

            try:
                cur_sw.execute("""
                    INSERT INTO programa_salud_links
                        (code, celular, nombre, programa, campana, whatsapp_destino)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (code, celular, nombre, programa, campana, WHATSAPP_DESTINO_PROGRAMAS))
                conn_sw.commit()

                enviar_sms_danaconnect_programa(celular, mensaje_final, link_corto)
                estado = "enviado"
                error_detalle = None
                enviados += 1
                try:
                    sms_aviso_programa(celular, "programa de salud")
                except Exception as e_aviso:
                    print(f"[WARN] No se pudo registrar sms_aviso para {celular}: {e_aviso}")
            except Exception as e:
                estado = "error"
                error_detalle = str(e)
                errores += 1

            cur.execute("""
                INSERT INTO programa_sms_envios (
                    paciente_programa_id, celular, nombre_paciente, programa,
                    mensaje_enviado, link_enviado, estado_envio, error_detalle, click_code
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (paciente_id, celular, nombre, programa, mensaje_final, link_corto, estado, error_detalle, code))
            conn.commit()

        cur_sw.close(); conn_sw.close(); connector_sw.close()

        return {"enviados": enviados, "omitidos": omitidos, "errores": errores}

    finally:
        cur.close()
        conn.close()
        connector.close()

# ──────────────────────────────────────────────────────────────
# ENDPOINT: POST /enviar-sms-programas
# Body: { "ids": [1, 2, 3] }
# ──────────────────────────────────────────────────────────────
def handle_enviar_sms_programas(request):
    if not validar_api_key(request):
        return (json.dumps({"error": "API Key invalida o ausente"}), 401, HEADERS)

    data = request.get_json(force=True, silent=True) or {}
    ids = data.get("ids")  # opcional: si no viene, se procesan TODOS los pendientes

    if ids is not None and not isinstance(ids, list):
        return (json.dumps({"error": "'ids', si se envía, debe ser una lista"}), 400, HEADERS)

    try:
        resultado = _enviar_sms_a_pacientes(ids)
        return (json.dumps(resultado, ensure_ascii=False), 200, HEADERS)
    except Exception as e:
        return (json.dumps({"error": str(e)}), 500, HEADERS)

# ──────────────────────────────────────────────────────────────
# REPORTE + ENVÍO MANUAL — GET/POST /reporte-sms-programas?key=...
# ──────────────────────────────────────────────────────────────
def _obtener_estados_click_programas(codes: list) -> dict:
    if not codes:
        return {}
    conn, connector = get_db_connection_sofi_whatsapp()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT code, clicked_at, click_count
            FROM programa_salud_links WHERE code = ANY(%s)
        """, (codes,))
        return {row[0]: {"clicked_at": row[1], "click_count": row[2]} for row in cur.fetchall()}
    finally:
        cur.close(); conn.close(); connector.close()

def handle_reporte_sms_programas(request):
    if not validar_key_dashboard(request):
        return ("<h1>403 - Acceso denegado</h1>", 403, HEADERS_HTML)

    key = request.args.get("key", "")
    mensaje_resultado = ""

    conn, connector = get_db_connection()
    cur = conn.cursor()

    if request.method == "POST":
        accion = request.form.get("accion")

        if accion == "guardar_config":
            mensaje_nuevo = request.form.get("mensaje_template", "").strip()
            link_nuevo    = request.form.get("link", "").strip()
            cur.execute("""
                INSERT INTO programa_sms_config (mensaje_template, link)
                VALUES (%s, %s)
            """, (mensaje_nuevo, link_nuevo))
            conn.commit()
            mensaje_resultado = '<p style="color:#16a34a;font-weight:600;">✅ Mensaje y link actualizados.</p>'

        elif accion == "enviar":
            ids_seleccionados = [int(i) for i in request.form.getlist("paciente_id")]
            if not ids_seleccionados:
                mensaje_resultado = '<p style="color:#dc2626;font-weight:600;">Selecciona al menos un paciente.</p>'
            else:
                resultado = _enviar_sms_a_pacientes(ids_seleccionados)
                if resultado.get("error"):
                    mensaje_resultado = f'<p style="color:#dc2626;font-weight:600;">{resultado["error"]}</p>'
                else:
                    mensaje_resultado = (
                        f'<p style="color:#16a34a;font-weight:600;">'
                        f'✅ {resultado["enviados"]} enviados, '
                        f'⏭️ {resultado["omitidos"]} omitidos (ya tenían envío previo), '
                        f'❌ {resultado["errores"]} con error.</p>'
                    )

    # ── Config vigente ──
    cur.execute("SELECT mensaje_template, link FROM programa_sms_config ORDER BY id DESC LIMIT 1")
    fila_config = cur.fetchone()
    mensaje_template_actual, link_actual = fila_config if fila_config else ("", "")

    # ── Pacientes candidatos con su estado de envío ──
    cur.execute("""
        SELECT pp.id, pp.nombre_paciente, pp.celular, pp.programa, pp.fecha_registro,
               se.estado_envio, se.enviado_at, se.click_code
        FROM pacientes_programas pp
        LEFT JOIN LATERAL (
            SELECT estado_envio, enviado_at, click_code
            FROM programa_sms_envios
            WHERE paciente_programa_id = pp.id
            ORDER BY enviado_at DESC LIMIT 1
        ) se ON TRUE
        ORDER BY pp.fecha_registro DESC
        LIMIT 500
    """)
    pacientes = cur.fetchall()

    cur.close()
    conn.close()
    connector.close()

    codes_presentes = [p[7] for p in pacientes if p[7]]
    estados_click = _obtener_estados_click_programas(codes_presentes)

    filas_html = ""
    for pid, nombre, celular, programa, fecha_reg, estado_envio, enviado_at, click_code in pacientes:
        click_info = estados_click.get(click_code)

        if click_info and click_info["clicked_at"]:
            badge = f'<span style="color:#16a34a;font-weight:600;">✅ Abrió el link ({click_info["clicked_at"].strftime("%d/%m/%Y %H:%M")})</span>'
            checkbox = ""
        elif estado_envio == "enviado":
            badge = f'<span style="color:#0072ce;font-weight:600;">📩 Enviado, sin abrir ({enviado_at.strftime("%d/%m/%Y %H:%M")})</span>'
            checkbox = ""
        elif estado_envio == "error":
            badge = f'<span style="color:#dc2626;font-weight:600;">❌ Error ({enviado_at.strftime("%d/%m/%Y %H:%M")})</span>'
            checkbox = f'<input type="checkbox" name="paciente_id" value="{pid}">'
        else:
            badge = '<span style="color:#94a3b8;">— Sin enviar</span>'
            checkbox = f'<input type="checkbox" name="paciente_id" value="{pid}">'

        filas_html += f"""
        <tr>
            <td>{checkbox}</td>
            <td>{nombre or ''}</td>
            <td>{celular or ''}</td>
            <td>{programa or ''}</td>
            <td>{fecha_reg.strftime('%d/%m/%Y') if fecha_reg else ''}</td>
            <td>{badge}</td>
        </tr>"""

    html_out = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex">
<link rel="icon" type="image/x-icon" href="/assets/favicon_sanitas.ico">
<title>SMS Programas de Salud — Sanitas</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; background: #f4f6f8; margin: 0; padding: 24px; color: #1f2937; }}
  h1 {{ color: #003b71; font-size: 20px; }}
  .card {{ background: white; border: 1px solid #e2e8f0; border-radius: 10px; padding: 18px; margin-bottom: 20px; }}
  label {{ display: block; font-size: 12px; font-weight: 600; color: #475569; margin: 10px 0 4px; }}
  textarea, input[type=text] {{ width: 100%; box-sizing: border-box; padding: 8px 10px; border: 1px solid #e2e8f0; border-radius: 6px; font-size: 14px; }}
  button {{ background: #0072ce; color: white; border: none; padding: 10px 18px; border-radius: 6px; cursor: pointer; font-weight: 600; margin-top: 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 8px; background: #f4f6f8; color: #475569; }}
  td {{ padding: 8px; border-bottom: 1px solid #e2e8f0; }}
</style>
</head>
<body>
  <h1>📲 SMS — Programas de Salud</h1>

  <div class="card">
    <h3>Mensaje y link vigentes</h3>
    <form method="POST">
      <input type="hidden" name="key" value="{html_lib_escape(key)}">
      <input type="hidden" name="accion" value="guardar_config">
      <label>Mensaje (usa {{Nombre}} para personalizar)</label>
      <textarea name="mensaje_template" rows="3">{html_lib_escape(mensaje_template_actual)}</textarea>
      <label>Link a incluir en el SMS</label>
      <input type="text" name="link" value="{html_lib_escape(link_actual)}">
      <button type="submit">Guardar mensaje y link</button>
    </form>
  </div>

  <div class="card">
    {mensaje_resultado}
    <h3>Pacientes ({len(pacientes)})</h3>
    <form method="POST">
      <input type="hidden" name="key" value="{html_lib_escape(key)}">
      <input type="hidden" name="accion" value="enviar">
      <table>
        <thead>
          <tr><th></th><th>Nombre</th><th>Celular</th><th>Programa</th><th>Registrado</th><th>Estado SMS</th></tr>
        </thead>
        <tbody>{filas_html}</tbody>
      </table>
      <button type="submit" onclick="return confirm('¿Enviar SMS a los pacientes seleccionados?');">Enviar SMS a seleccionados</button>
    </form>
  </div>
</body>
</html>"""

    return (html_out, 200, HEADERS_HTML)


def html_lib_escape(texto):
    import html as html_lib
    return html_lib.escape(texto or "")

def sms_aviso_programa(celular, descripcion):
    """
    Registra el envío en sofi_sms_avisos (base sofi_whatsapp), la misma
    tabla que usa sofi-apis para su estadística de gasto mensual de SMS.
    """
    conn, connector = get_db_connection_sofi_whatsapp()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO sofi_sms_avisos (celular, descripcion, fecha)
            VALUES (%s, %s, %s)
        """, (celular, descripcion, datetime.now(LIMA_TZ).strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        cur.close()
        conn.close()
        connector.close()

