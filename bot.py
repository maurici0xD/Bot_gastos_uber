import os
import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

import uvicorn
from fastapi import FastAPI, Body
from pydantic import BaseModel, Field
from pyrogram import Client

# --- 1. Configuración Básica ---

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

# Lee las credenciales de Telegram
try:
    API_ID = int(os.getenv("TELEGRAM_API_ID"))
    API_HASH = os.getenv("TELEGRAM_API_HASH")
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not all([API_ID, API_HASH, TOKEN]):
         raise ValueError("Variables de entorno de Telegram no configuradas.")
except (ValueError, TypeError):
    logger.critical("Error: Asegúrate de configurar tus variables de entorno.")
    exit()

# Creamos la instancia del cliente de Pyrogram
pyrogram_client = Client(
    "data/traccar_test_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=TOKEN
)

# --- 2. Lógica de FastAPI y Traccar (VERSIÓN FINAL) ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestiona el ciclo de vida de Pyrogram junto con FastAPI."""
    logger.info("Iniciando cliente de Pyrogram...")
    async with pyrogram_client:
        logger.info("Cliente de Pyrogram listo.")
        yield
    logger.info("Cliente de Pyrogram detenido.")

api = FastAPI(lifespan=lifespan)

# --- INICIO DE MODELOS DE DATOS ANIDADOS ---
# Creamos un "molde" que coincide exactamente con el JSON que envía Traccar.

class Coords(BaseModel):
    latitude: float
    longitude: float
    speed: float

class Location(BaseModel):
    coords: Coords
    timestamp: datetime

class TraccarPayload(BaseModel):
    location: Location
    device_id: str = Field(..., alias='device_id')

# --- FIN DE MODELOS DE DATOS ---


@api.post("/api/traccar")
async def receive_traccar_data(payload: TraccarPayload = Body(...)):
    """
    Este es el 'receptor' final. Lee el cuerpo JSON de la petición
    y lo valida contra nuestro modelo TraccarPayload.
    """
    # Extraemos los datos del payload validado
    user_id_str = payload.device_id
    lat = payload.location.coords.latitude
    lon = payload.location.coords.longitude
    
    logger.info(f"¡Éxito! Datos JSON procesados para ID {user_id_str}: Lat={lat}, Lon={lon}")
    
    try:
        user_id = int(user_id_str)
        
        # Enviamos la notificación de confirmación a Telegram
        await pyrogram_client.send_message(
            chat_id=user_id,
            text=f"✅ Coordenada recibida y procesada:\nLat: `{lat}`\nLon: `{lon}`"
        )
    except Exception as e:
        logger.error(f"No se pudo enviar el mensaje de confirmación a Telegram: {e}")
        
    return {"status": "ok"}


# --- 3. Arranque del Servidor ---

if __name__ == "__main__":
    logger.info("Iniciando servidor de prueba para Traccar...")
    uvicorn.run(api, host="0.0.0.0", port=9091)
