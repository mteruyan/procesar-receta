import functions_framework
import os
import json
import requests
from datetime import datetime, timezone, timedelta
from google.cloud import documentai, storage
import vertexai
from vertexai.generative_models import GenerativeModel
import pg8000
from google.cloud.sql.connector import Connector

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
GEMINI_MODEL     = "gemini-2.5-flash-lite"
UMBRAL_CONFIANZA = 0.9                           # por debajo → manuscrito → atención humana
LIMA_TZ          = timezone(timedelta(hours=-5)) # UTC-5, sin DST

# MIME types soportados por Document AI
MIME_PDF  = "application/pdf"
MIME_JPEG = "image/jpeg"
MIME_PNG  = "image/png"
MIME_WEBP = "image/webp"
MIME_TIFF = "image/tiff"
MIME_GIF  = "image/gif"

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
    return conn, connector

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

# ──────────────────────────────────────────────────────────────
# PUNTO DE ENTRADA
# Input JSON: { "celular", "ruta", "doi", "chat_id" }
# ──────────────────────────────────────────────────────────────
@functions_framework.http
def procesar_receta(request):

    # Preflight CORS
    if request.method == "OPTIONS":
        return ("", 204, HEADERS)

    # Validar API Key
    if not validar_api_key(request):
        return (json.dumps({"error": "API Key invalida o ausente"}), 401, HEADERS)

    try:
        #data = request.get_json(silent=True)
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
        now_lima       = datetime.now(LIMA_TZ)
        archivo_nombre = f"recetas-entrantes/{celular}_{now_lima.strftime('%Y%m%d_%H%M%S')}.{ext}"
        guardar_en_storage(archivo_bytes, archivo_nombre, mime_type)

        # ── PASO 4: Document AI — OCR + nivel de confianza ────
        texto_extraido, confianza_ocr, tiene_manuscrito = procesar_con_document_ai(
            archivo_bytes, mime_type
        )
        print(f"[INFO] OCR — confianza: {confianza_ocr} | manuscrito: {tiene_manuscrito}")
        print(f"[INFO] Texto extraido (200 chars): {texto_extraido[:200]}")

        # Si confianza muy baja, derivar a humano sin procesar con Gemini
        if tiene_manuscrito:
            print("[INFO] Confianza baja — derivando a atencion humana sin analisis Gemini")
            receta_id = guardar_receta_manuscrita(
                celular, doi, chat_id, archivo_nombre, confianza_ocr
            )
            return (json.dumps({
                "status":                 "revision_humana",
                "receta_id":              receta_id,
                "confianza_ocr":          confianza_ocr,
                "tiene_manuscrito":       True,
                "atencion_humana":        True,
                "motivo_atencion_humana": "TEXTO MANUSCRITO O CONFIANZA OCR BAJA (< 90%)",
                "respuesta_paciente": (
                    f"⏳ Tu receta #{receta_id} esta siendo revisada por un asesor. "
                    f"Te contactaremos en los proximos minutos."
                )
            }, ensure_ascii=False), 200, HEADERS)

        # ── PASO 5: Gemini — analizar y estructurar receta ────
        analisis = analizar_con_gemini(texto_extraido)
        print(f"[INFO] Estado cobertura: {analisis.get('estado_cobertura')}")

        # Enriquecer analisis con datos del OCR
        analisis["confianza_ocr"]    = confianza_ocr
        analisis["tiene_manuscrito"] = tiene_manuscrito

        # Determinar si requiere atencion humana
        if analisis.get("requiere_revision", False):
            analisis["atencion_humana"]        = True
            analisis["motivo_atencion_humana"] = analisis.get("motivo_revision", "").upper()
        else:
            analisis["atencion_humana"]        = False
            analisis["motivo_atencion_humana"] = ""

        # ── PASO 6: Guardar en base de datos ──────────────────
        receta_id = guardar_en_base_datos(
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
            "datos_extraidos": {
                "paciente":      analisis.get("nombre_paciente"),
                "dni":           analisis.get("dni_paciente"),
                "medico":        analisis.get("nombre_medico"),
                "cmp":           analisis.get("cmp_medico"),
                "fecha_emision": analisis.get("fecha_emision"),
                "aseguradora":   analisis.get("aseguradora"),
                "diagnosticos":  analisis.get("diagnosticos", []),
                "medicamentos":  analisis.get("medicamentos", [])
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
def analizar_con_gemini(texto_receta: str) -> dict:
    vertexai.init(project=PROJECT_ID, location="us-central1")
    modelo = GenerativeModel(GEMINI_MODEL)

    # ── PROMPT — editar aquí para mejorar la extracción ──────
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
- Para principio_activo: si ves la palabra DCI antes del nombre, usar ese valor.
  Si empieza con "ACIDO", omitir "ACIDO" (ej: "ACIDO ACETILSALICILICO" -> "ACETILSALICILICO").
- cantidad: calcular dosis_diaria x duracion_dias.
- receta_extendida="S" si el tratamiento supera 30 dias, sino "N"."""
    # ── FIN PROMPT ────────────────────────────────────────────

    respuesta       = modelo.generate_content(prompt)
    texto_respuesta = respuesta.text.strip()

    # Limpiar markdown si Gemini lo incluye
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
    archivo_url: str, confianza_ocr: float
) -> int:
    now_lima        = datetime.now(LIMA_TZ)
    conn, connector = get_db_connection()
    cur             = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO recetas (
                celular, doi, chat_id,
                canal, archivo_url, estado,
                confianza_ocr, requiere_revision, motivo_revision,
                fecha_recepcion, created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            celular, doi, chat_id,
            "whatsapp", archivo_url, "revision_humana",
            confianza_ocr, True, "TEXTO MANUSCRITO O CONFIANZA OCR BAJA",
            now_lima, now_lima
        ))
        receta_id = cur.fetchone()[0]

        cur.execute("""
            INSERT INTO receta_eventos (receta_id, evento, detalle, created_at)
            VALUES (%s,%s,%s,%s)
        """, (
            receta_id, "recibida",
            f"Manuscrito detectado. Confianza OCR: {confianza_ocr}",
            now_lima
        ))

        conn.commit()
        return receta_id

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
    now_lima        = datetime.now(LIMA_TZ)
    conn, connector = get_db_connection()
    cur             = conn.cursor()

    try:
        # Tabla recetas
        cur.execute("""
            INSERT INTO recetas (
                celular, doi, chat_id, canal,
                nombre_paciente, dni_paciente,
                nombre_medico, cmp_medico, fecha_emision,
                aseguradora, archivo_url, estado,
                confianza_ocr, requiere_revision, motivo_revision,
                fecha_recepcion, created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            celular, doi, chat_id, "whatsapp",
            analisis.get("nombre_paciente"),
            analisis.get("dni_paciente"),
            analisis.get("nombre_medico"),
            analisis.get("cmp_medico"),
            analisis.get("fecha_emision") or None,
            analisis.get("aseguradora"),
            archivo_url,
            analisis.get("estado_cobertura", "pendiente"),
            confianza_ocr,
            analisis.get("requiere_revision", True),
            analisis.get("motivo_atencion_humana"),
            now_lima, now_lima
        ))
        receta_id = cur.fetchone()[0]

        # Tabla receta_diagnosticos
        for diag in analisis.get("diagnosticos", []):
            cur.execute("""
                INSERT INTO receta_diagnosticos (receta_id, codigo, descripcion, created_at)
                VALUES (%s,%s,%s,%s)
            """, (
                receta_id,
                diag.get("codigo"),
                diag.get("descripcion"),
                now_lima
            ))

        # Tabla receta_medicamentos
        for med in analisis.get("medicamentos", []):
            cur.execute("""
                INSERT INTO receta_medicamentos (
                    receta_id, nombre_medicamento, principio_activo,
                    dosis, frecuencia, duracion, cantidad,
                    cobertura, receta_extendida, en_petitorio, alerta,
                    created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                receta_id,
                med.get("nombre"),
                med.get("principio_activo"),
                med.get("dosis"),
                med.get("frecuencia"),
                med.get("duracion"),
                med.get("cantidad"),
                med.get("cobertura"),
                med.get("receta_extendida", "N"),
                med.get("en_petitorio", True),
                med.get("alerta"),
                now_lima
            ))

        # Tabla receta_eventos
        cur.execute("""
            INSERT INTO receta_eventos (receta_id, evento, detalle, created_at)
            VALUES (%s,%s,%s,%s)
        """, (
            receta_id, "recibida",
            f"Confianza OCR: {confianza_ocr} | Estado: {analisis.get('estado_cobertura')}",
            now_lima
        ))

        conn.commit()
        return receta_id

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
            lineas_meds += f"  {emoji} {nombre}\n"
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
        msg  = f"✅ Receta #{receta_id} aprobada\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"👤 {analisis.get('nombre_paciente', 'N/A')}\n"
        msg += f"🩺 Dr. {analisis.get('nombre_medico', 'N/A')}\n"
        msg += f"📅 Emisión: {analisis.get('fecha_emision', 'N/A')}\n"
        if lineas_diag:
            msg += f"\n📋 Diagnóstico:\n{lineas_diag}"
        if lineas_meds:
            msg += f"\n💊 Medicamentos:\n{lineas_meds}"
        msg += f"\nEn breve recibirás la confirmación de tu pedido."
        return msg
    else:
        msg  = f"⚠️ Receta #{receta_id} en revisión\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"👤 {analisis.get('nombre_paciente', 'N/A')}\n"
        msg += f"🩺 Dr. {analisis.get('nombre_medico', 'N/A')}\n"
        if lineas_meds:
            msg += f"\n💊 Medicamentos:\n{lineas_meds}"
        msg += f"\nUn asesor se comunicará contigo pronto."
        return msg