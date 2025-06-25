import os
import logging
import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
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

# El modelo de datos ya no es necesario para la validación del endpoint,
# pero lo dejamos como buena práctica para documentar la estructura de datos.
class TraccarData(BaseModel):
    id: str
    lat: float
    lon: float
    timestamp: int
    speed: float = 0.0

# --- MODIFICACIÓN FINAL: Se cambia la firma de la función ---
# En lugar de usar 'Depends()', definimos cada parámetro que esperamos en la URL.
# FastAPI se encargará de extraerlos automáticamente.
@api.post("/api/traccar")
async def receive_traccar_data(
    id: str,
    lat: float,
    lon: float,
    timestamp: int,
    speed: float = 0.0
):
    """
    Este es el 'receptor'. Se activa cuando Traccar envía una ubicación.
    """
    logger.info(f"¡Ping de Traccar recibido! Datos: id={id}, lat={lat}, lon={lon}")
    
    try:
        user_id = int(id)
        
        # Enviamos una notificación al usuario por Telegram para confirmar la recepción
        await pyrogram_client.send_message(
            chat_id=user_id,
            text=f"✅ Coordenada recibida de Traccar:\nLat: `{lat}`\nLon: `{lon}`"
        )
    except Exception as e:
        logger.error(f"No se pudo enviar el mensaje de confirmación a Telegram: {e}")
        
    return {"status": "ok"}


# --- 3. Arranque del Servidor ---

if __name__ == "__main__":
    logger.info("Iniciando servidor de prueba para Traccar...")
    uvicorn.run(api, host="0.0.0.0", port=9091)
