import functions_framework
import json
import requests
import os
from datetime import datetime
from google.cloud import documentai, storage
import vertexai
from vertexai.generative_models import GenerativeModel
import pg8000
from google.cloud.sql.connector import Connector

# ── Configuración ──────────────────────────────────────────
PROJECT_ID        = "sanitas-recetas-prod"
LOCATION          = "us"
PROCESSOR_ID      = "6837fb55f199826a"
BUCKET_NAME       = "sanitas-recetas-archivos"
DB_NAME           = "recetas"
DB_USER           = "recetas_user"
DB_PASS           = os.environ.get("DB_PASS")
INSTANCE_CONN     = "sanitas-recetas-prod:us-central1:sanitas-recetas-db"
GEMINI_MODEL      = "gemini-2.5-flash-lite"
UMBRAL_CONFIANZA  = 0.9

# ─────────────────────────────────────────────
# CONEXIÓN
# ─────────────────────────────────────────────
def get_connection():
    return psycopg2.connect(
        dbname="sofi_whatsapp",
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        host=f"/cloudsql/{os.environ['CLOUD_SQL_CONNECTION_NAME']}"
    )

# ─────────────────────────────────────────────
# VALIDACIÓN API KEY
# ─────────────────────────────────────────────
def validar_api_key(request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("ApiKey "):
        return False
    key = auth.split(" ", 1)[1]
    return key == os.environ.get("API_KEY", "")

# ─────────────────────────────────────────────
# HEADERS
# ─────────────────────────────────────────────
HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Content-Type": "application/json"
}

# ── Conexión a BD via Cloud SQL Connector ──────────────────
def get_db_connection():
    connector = Connector()
    conn = connector.connect(
        INSTANCE_CONN,
        "pg8000",
        user=DB_USER,
        password=DB_PASS,
        db=DB_NAME
    )
    return conn, connector

# ── Punto de entrada principal ─────────────────────────────
@functions_framework.http
def procesar_receta(request):
    if request.method == "OPTIONS":
        return ("", 204, HEADERS)
    
    if not validar_api_key(request):
        return (json.dumps({"error": "API Key invalida"}), 401, HEADERS)
    
    try:
        data = request.get_json(silent=True)
        if not data:
            return json.dumps({"error": "No se recibió payload"}), 400

        numero_paciente = data.get("from", "")
        archivo_url     = data.get("media_url", "")
        tipo_archivo    = data.get("media_type", "image/jpeg")

        if not archivo_url:
            return json.dumps({"error": "No se recibió archivo"}), 400

        print(f"[INFO] Receta recibida de {numero_paciente}")

        # 1. Descargar archivo
        archivo_bytes  = descargar_archivo(archivo_url)

        # 2. Guardar en Storage
        archivo_nombre = f"recetas-entrantes/{numero_paciente}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        guardar_en_storage(archivo_bytes, archivo_nombre)

        # 3. Document AI — OCR + confidence
        texto_extraido, confianza_ocr, tiene_manuscrito = procesar_con_document_ai(archivo_bytes, tipo_archivo)
        print(f"[INFO] Texto extraído (confianza: {confianza_ocr}): {texto_extraido[:200]}...")

        # 4. Gemini — análisis
        analisis = analizar_con_gemini(texto_extraido)
        print(f"[INFO] Análisis: {analisis.get('estado_cobertura')}")

        # 5. Aplicar lógica de manuscrito
        if tiene_manuscrito:
            analisis["requiere_revision"]  = True
            analisis["atencion_humana"]    = True
            analisis["motivo_atencion_humana"] = "Texto manuscrito detectado (confianza OCR < 90%)"
        else:
            analisis["atencion_humana"]        = analisis.get("requiere_revision", False)
            analisis["motivo_atencion_humana"] = analisis.get("motivo_revision", "") if analisis.get("requiere_revision") else ""

        analisis["confianza_ocr"]    = confianza_ocr
        analisis["tiene_manuscrito"] = tiene_manuscrito

        # 6. Guardar en BD
        receta_id = guardar_en_base_datos(numero_paciente, archivo_nombre, analisis, confianza_ocr)

        # 7. Construir resumen legible
        resumen = construir_resumen(analisis)

        # 8. Respuesta al paciente
        respuesta_paciente = generar_respuesta(analisis, receta_id)

        return json.dumps({
            "status":             "ok",
            "receta_id":          receta_id,
            "confianza_ocr":      confianza_ocr,
            "tiene_manuscrito":   tiene_manuscrito,
            "atencion_humana":    analisis["atencion_humana"],
            "motivo_atencion_humana": analisis["motivo_atencion_humana"],
            "resumen_receta":     resumen,
            "datos_extraidos":    {
                "paciente":       analisis.get("nombre_paciente"),
                "dni":            analisis.get("dni_paciente"),
                "medico":         analisis.get("nombre_medico"),
                "cmp":            analisis.get("cmp_medico"),
                "fecha_emision":  analisis.get("fecha_emision"),
                "diagnostico":    analisis.get("diagnostico"),
                "medicamentos":   analisis.get("medicamentos", [])
            },
            "estado_cobertura":   analisis.get("estado_cobertura"),
            "respuesta_paciente": respuesta_paciente
        }, ensure_ascii=False), 200

    except Exception as e:
        print(f"[ERROR] {str(e)}")
        return json.dumps({"error": str(e)}), 500


# ── Funciones auxiliares ───────────────────────────────────

def descargar_archivo(url):
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.content


def guardar_en_storage(contenido, nombre_archivo):
    cliente = storage.Client()
    bucket  = cliente.bucket(BUCKET_NAME)
    blob    = bucket.blob(nombre_archivo)
    blob.upload_from_string(contenido, content_type="image/jpeg")
    print(f"[INFO] Guardado en gs://{BUCKET_NAME}/{nombre_archivo}")


def procesar_con_document_ai(archivo_bytes, mime_type="image/jpeg"):
    """
    Extrae texto y calcula confianza promedio.
    Si confianza < UMBRAL_CONFIANZA se asume texto manuscrito.
    """
    cliente   = documentai.DocumentProcessorServiceClient()
    nombre    = cliente.processor_path(PROJECT_ID, LOCATION, PROCESSOR_ID)
    documento = documentai.RawDocument(content=archivo_bytes, mime_type=mime_type)
    request   = documentai.ProcessRequest(name=nombre, raw_document=documento)
    resultado = cliente.process_document(request=request)
    doc       = resultado.document

    # Calcular confianza promedio de todos los tokens
    confidences = []
    for page in doc.pages:
        for token in page.tokens:
            if hasattr(token, "layout") and hasattr(token.layout, "confidence"):
                confidences.append(token.layout.confidence)

    confianza_ocr    = round(sum(confidences) / len(confidences), 4) if confidences else 1.0
    tiene_manuscrito = confianza_ocr < UMBRAL_CONFIANZA

    print(f"[INFO] Confianza OCR promedio: {confianza_ocr} | Manuscrito: {tiene_manuscrito}")
    return doc.text, confianza_ocr, tiene_manuscrito


def analizar_con_gemini(texto_receta):
    vertexai.init(project=PROJECT_ID, location="us-central1")
    modelo = GenerativeModel(GEMINI_MODEL)

    prompt = f"""Eres un sistema de análisis de recetas médicas para Sanitas Perú.
Analiza el siguiente texto extraído de una receta médica y responde ÚNICAMENTE con un JSON válido.

TEXTO DE LA RECETA:
{texto_receta}

Responde con este JSON exacto (sin markdown, sin explicaciones):
{{
  "nombre_paciente": "",
  "dni_paciente": "",
  "nombre_medico": "",
  "cmp_medico": "",
  "fecha_emision": "",
  "diagnostico": "",
  "medicamentos": [
    {{
      "nombre": "",
      "dosis": "",
      "frecuencia": "",
      "duracion": "",
      "cobertura": "cubierto|parcial|no_cubierto|revisar",
      "alerta": ""
    }}
  ],
  "estado_cobertura": "aprobado|revision|rechazado",
  "requiere_revision": true,
  "motivo_revision": "",
  "confianza": 0.95
}}

Reglas:
- Si la receta tiene más de 30 días, requiere_revision = true y motivo = "Receta vencida"
- Si hay medicamentos controlados, requiere_revision = true
- Si no se ve firma o sello, requiere_revision = true
- cobertura "revisar" cuando no puedas determinarlo con certeza
- confianza entre 0 y 1 según claridad del texto"""

    respuesta       = modelo.generate_content(prompt)
    texto_respuesta = respuesta.text.strip()

    if texto_respuesta.startswith("```"):
        texto_respuesta = texto_respuesta.split("```")[1]
        if texto_respuesta.startswith("json"):
            texto_respuesta = texto_respuesta[4:]

    return json.loads(texto_respuesta)


def construir_resumen(analisis):
    """Genera un resumen legible de la receta para el asesor."""
    meds = analisis.get("medicamentos", [])
    lineas_meds = []
    for m in meds:
        cobertura_emoji = {
            "cubierto":    "✅",
            "parcial":     "⚠️",
            "no_cubierto": "❌",
            "revisar":     "🔍"
        }.get(m.get("cobertura", "revisar"), "🔍")

        linea = (f"{cobertura_emoji} {m.get('nombre', 'N/A')} — "
                 f"{m.get('dosis', '')} {m.get('frecuencia', '')} "
                 f"por {m.get('duracion', 'N/A')}")
        if m.get("alerta"):
            linea += f" ⚠️ {m.get('alerta')}"
        lineas_meds.append(linea)

    manuscrito_texto = "⚠️ SÍ — requiere validación humana" if analisis.get("tiene_manuscrito") else "✅ No"

    resumen = {
        "paciente":       f"{analisis.get('nombre_paciente', 'N/A')} (DNI: {analisis.get('dni_paciente', 'N/A')})",
        "medico":         f"{analisis.get('nombre_medico', 'N/A')} (CMP: {analisis.get('cmp_medico', 'N/A')})",
        "fecha_emision":  analisis.get("fecha_emision", "N/A"),
        "diagnostico":    analisis.get("diagnostico", "N/A"),
        "medicamentos":   lineas_meds,
        "cobertura":      analisis.get("estado_cobertura", "N/A").upper(),
        "confianza_ocr":  f"{round(analisis.get('confianza_ocr', 0) * 100, 1)}%",
        "manuscrito":     manuscrito_texto,
        "atencion_humana": "🚨 SÍ" if analisis.get("atencion_humana") else "🤖 Automático",
        "motivo":         analisis.get("motivo_atencion_humana", "")
    }
    return resumen


def guardar_en_base_datos(numero_paciente, archivo_url, analisis, confianza_ocr):
    conn, connector = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO recetas (
            numero_paciente, nombre_paciente, dni_paciente,
            nombre_medico, cmp_medico, fecha_emision,
            diagnostico, archivo_url, estado,
            confianza_ocr, requiere_revision, motivo_revision
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
    """, (
        numero_paciente,
        analisis.get("nombre_paciente"),
        analisis.get("dni_paciente"),
        analisis.get("nombre_medico"),
        analisis.get("cmp_medico"),
        analisis.get("fecha_emision") or None,
        analisis.get("diagnostico"),
        archivo_url,
        analisis.get("estado_cobertura", "pendiente"),
        confianza_ocr,
        analisis.get("requiere_revision", True),
        analisis.get("motivo_atencion_humana")
    ))
    receta_id = cur.fetchone()[0]

    for med in analisis.get("medicamentos", []):
        cur.execute("""
            INSERT INTO receta_medicamentos (
                receta_id, nombre_medicamento, dosis,
                frecuencia, duracion, cobertura, alerta
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (
            receta_id,
            med.get("nombre"),
            med.get("dosis"),
            med.get("frecuencia"),
            med.get("duracion"),
            med.get("cobertura"),
            med.get("alerta")
        ))

    cur.execute("""
        INSERT INTO receta_eventos (receta_id, evento, detalle)
        VALUES (%s,%s,%s)
    """, (receta_id, "recibida", f"Confianza OCR: {confianza_ocr} | Manuscrito: {analisis.get('tiene_manuscrito')}"))

    conn.commit()
    cur.close()
    conn.close()
    connector.close()
    return receta_id


def generar_respuesta(analisis, receta_id):
    estado = analisis.get("estado_cobertura")
    meds   = analisis.get("medicamentos", [])
    lista  = ", ".join([m.get("nombre", "") for m in meds])

    if analisis.get("atencion_humana"):
        return (f"⏳ Tu receta #{receta_id} está siendo revisada por un asesor. "
                f"Te contactaremos en los próximos minutos.")
    elif estado == "aprobado":
        return (f"✅ Tu receta #{receta_id} fue procesada exitosamente. "
                f"Medicamentos cubiertos: {lista}. "
                f"En breve recibirás la confirmación de tu pedido.")
    else:
        return (f"⚠️ Tu receta #{receta_id} requiere revisión por un especialista. "
                f"Un asesor se comunicará contigo pronto.")
