import sys
import vertexai
from vertexai.generative_models import GenerativeModel

# Configuración de tu entorno de GCP
PROJECT_ID = "sanitas-recetas-prod"
LOCATION = "us"  # Cambia la región si tu recurso está en otra ubicación
MODEL_NAME = "gemini-3.1-flash-lite"

def test_model_access():
    print(f"=== PRUEBA DE CONEXIÓN A VERTEX AI ===")
    print(f"Proyecto: {PROJECT_ID}")
    print(f"Ubicación: {LOCATION}")
    print(f"Modelo a probar: {MODEL_NAME}\n")

    try:
        # 1. Inicializar el SDK de Vertex AI
        vertexai.init(project=PROJECT_ID, location=LOCATION)
        model = GenerativeModel(MODEL_NAME)

        # 2. Prueba 1: Generación de contenido simple
        print("[1/2] Probando llamada simple (generate_content)...")
        response = model.generate_content("Responde únicamente con la palabra 'OK' si recibes este mensaje.")
        print(f" Respuesta recibida: {response.text.strip()}\n")

        # 3. Prueba 2: Conversación multi-turno (validación de soporte de chat y firmas)
        print("[2/2] Probando sesión de chat multi-turno (start_chat)...")
        chat = model.start_chat()
        chat.send_message("Guarda este número en memoria: 42")
        chat_response = chat.send_message("¿Qué número te pedí que guardaras?")
        print(f" Respuesta del chat: {chat_response.text.strip()}\n")

        print(" RESULTADO: El modelo 'gemini-3.1-flash-lite' está habilitado y funcionando correctamente.")

    except Exception as e:
        print(f" ERROR: No se pudo conectar al modelo.")
        print(f"Detalle del error:\n{e}")
        sys.exit(1)

if __name__ == "__main__":
    test_model_access()