import os
import logging
import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request # <-- Se importa Request
from pydantic import BaseModel
from pyrogram import Client

# --- 1. Configuración Básica ---

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

# Lee las credenciales de Telegram desde las variables de entorno
try:
    API_ID = int(os.getenv("TELEGRAM_API_ID"))
    API_HASH = os.getenv("TELEGRAM_API_HASH")
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not all([API_ID, API_HASH, TOKEN]):
         raise ValueError("Variables de entorno de Telegram no configuradas.")
except (ValueError, TypeError):
    logger.critical("Error: Asegúrate de configurar tus variables de entorno (API_ID, API_HASH, TOKEN).")
    exit()

# Creamos la instancia del cliente de Pyrogram
pyrogram_client = Client(
    "data/traccar_test_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=TOKEN
)

# --- 2. Lógica de FastAPI y Traccar ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestiona el ciclo de vida de Pyrogram junto con FastAPI."""
    logger.info("Iniciando cliente de Pyrogram...")
    async with pyrogram_client:
        logger.info("Cliente de Pyrogram listo.")
        yield
    logger.info("Cliente de Pyrogram detenido.")

# Creamos la instancia de FastAPI y le asignamos nuestro manejador de ciclo de vida
api = FastAPI(lifespan=lifespan)


# --- MODIFICACIÓN DE DEPURACIÓN: Capturar la petición cruda ---
# Cambiamos la firma para recibir el objeto Request completo.
@api.post("/api/traccar")
async def receive_traccar_data(request: Request):
    """
    Este es el 'receptor' en modo depuración.
    Captura y muestra toda la información de la petición entrante.
    """
    logger.info("="*20 + " PETICIÓN DE TRACCAR RECIBIDA " + "="*20)
    
    # 1. Imprimir los parámetros de la URL
    query_params = request.query_params
    logger.info(f"Parámetros en la URL (Query Params): {query_params}")

    # 2. Imprimir el cuerpo (body) de la petición
    body = await request.body()
    try:
        body_text = body.decode('utf-8')
        logger.info(f"Cuerpo de la Petición (Body): {body_text}")
    except UnicodeDecodeError:
        logger.info(f"Cuerpo de la Petición (crudo, no es texto): {body}")

    # 3. Imprimir las cabeceras (headers)
    headers = request.headers
    logger.info(f"Cabeceras (Headers): {headers}")
    
    logger.info("="*20 + " FIN DE LA PETICIÓN " + "="*20)

    # Respondemos siempre 'ok' para que Traccar no muestre errores.
    return {"status": "ok"}


# --- 3. Arranque del Servidor ---

if __name__ == "__main__":
    logger.info("Iniciando servidor de prueba para Traccar (MODO DEPURACIÓN)...")
    uvicorn.run(api, host="0.0.0.0", port=9091)
