import os
import logging
import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Depends
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
# --- MODIFICACIÓN CLAVE: Se añade 'data/' al nombre de la sesión ---
pyrogram_client = Client(
    "data/traccar_test_session", # El archivo de sesión se guardará en la carpeta 'data'
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

# Definimos la estructura de datos que esperamos de Traccar
class TraccarData(BaseModel):
    id: str
    lat: float
    lon: float
    timestamp: int
    speed: float = 0.0

@api.post("/api/traccar")
async def receive_traccar_data(data: TraccarData = Depends()):
    """
    Este es el 'receptor'. Se activa cuando Traccar envía una ubicación.
    """
    # Imprimimos en la consola para confirmar que recibimos los datos
    logger.info(f"¡Ping de Traccar recibido! Datos: id={data.id}, lat={data.lat}, lon={data.lon}")
    
    try:
        user_id = int(data.id)
        
        # Enviamos una notificación al usuario por Telegram para confirmar la recepción
        await pyrogram_client.send_message(
            chat_id=user_id,
            text=f"✅ Coordenada recibida de Traccar:\nLat: `{data.lat}`\nLon: `{data.lon}`"
        )
    except Exception as e:
        logger.error(f"No se pudo enviar el mensaje de confirmación a Telegram: {e}")
        
    # Le respondemos a la app Traccar que todo está bien
    return {"status": "ok"}


# --- 3. Arranque del Servidor ---

if __name__ == "__main__":
    logger.info("Iniciando servidor de prueba para Traccar...")
    # Uvicorn se encargará de ejecutar nuestra app FastAPI y su ciclo de vida
    # Nota: Asegúrate que el puerto coincida con docker-compose.yml
    uvicorn.run(api, host="0.0.0.0", port=9091)
