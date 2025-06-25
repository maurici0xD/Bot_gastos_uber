import os
import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

import uvicorn
from fastapi import FastAPI, Body
from pydantic import BaseModel, Field
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

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

# --- INICIO DE MODELOS DE DATOS ANIDADOS (ACTUALIZADOS) ---

class Coords(BaseModel):
    latitude: float
    longitude: float
    speed: float # La velocidad en nudos

class Battery(BaseModel):
    level: float
    is_charging: bool = Field(..., alias='is_charging')

class Location(BaseModel):
    coords: Coords
    timestamp: datetime
    battery: Battery # <-- Se añade el modelo de batería

class TraccarPayload(BaseModel):
    location: Location
    # El device_id es el identificador del dispositivo que configuramos en la app Traccar
    device_id: str = Field(..., alias='device_id')

# --- FIN DE MODELOS DE DATOS ---

# --- INICIO DE MANEJADORES DE TELEGRAM ---

async def setup_command_handler(client, message: Message):
    """
    Envía las instrucciones detalladas para configurar la app Traccar Client.
    """
    user_id = message.from_user.id
    instructions = (
        "**Configuración de Traccar Client para el Bot**\n\n"
        "Para que pueda recibir tu ubicación con alta precisión, sigue estos pasos:\n\n"
        "1. **Descarga la App:**\n"
        "   - [Android: Traccar Client en Google Play](https://play.google.com/store/apps/details?id=org.traccar.client)\n"
        "   - [iOS: Traccar Client en App Store](https://apps.apple.com/us/app/traccar-client/id843156974)\n\n"
        "2. **Configura la App:**\n"
        "   - **Identificador del dispositivo:** `ESTE ES EL PASO MÁS IMPORTANTE.` Debes poner tu ID de Telegram aquí. Tu ID es: `{user_id}`\n"
        "   - **URL del servidor:** `http://mauinternetservice.ddns.net:9091/api/traccar`\n"
        "   - **Frecuencia:** `10` (en segundos, es un buen comienzo)\n"
        "   - **Distancia:** `0`\n"
        "   - **Ángulo:** `0`\n\n"
        "3. **Activa el Servicio:**\n"
        "   - Una vez configurada, pulsa el interruptor para activar el 'Estado del servicio'. La app comenzará a enviar tu ubicación al bot.\n\n"
        "¡Y listo! Ya puedes usar los comandos del bot para iniciar tu jornada."
    )
    await message.reply(instructions, disable_web_page_preview=True)

# --- FIN DE MANEJADORES DE TELEGRAM ---


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestiona el ciclo de vida de Pyrogram junto con FastAPI."""
    logger.info("Iniciando cliente de Pyrogram...")
    # Registramos los handlers antes de iniciar el cliente
    pyrogram_client.add_handler(MessageHandler(setup_command_handler, filters.command("setup") & filters.private))
    
    async with pyrogram_client:
        logger.info("Cliente de Pyrogram listo.")
        yield
    logger.info("Cliente de Pyrogram detenido.")

api = FastAPI(lifespan=lifespan)


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
    
    # Extraemos velocidad y datos de la batería
    speed_knots = payload.location.coords.speed
    speed_kmh = speed_knots * 1.852  # Convertimos nudos a km/h
    
    battery_level = payload.location.battery.level * 100  # Convertimos a porcentaje
    is_charging = payload.location.battery.is_charging
    charging_emoji = "⚡" if is_charging else ""
    
    logger.info(f"¡Éxito! Datos procesados para ID {user_id_str}: Lat={lat}, Lon={lon}, Vel={speed_kmh:.1f} km/h, Bat={battery_level:.0f}%")
    
    try:
        user_id = int(user_id_str)
        
        # Creamos el nuevo texto del mensaje con toda la información
        message_text = (
            f"✅ **Coordenada Recibida**\n\n"
            f"📍 **Ubicación:**\n   - Lat: `{lat}`\n   - Lon: `{lon}`\n"
            f"🚗 **Velocidad:** `{speed_kmh:.1f} km/h`\n"
            f"🔋 **Batería:** `{battery_level:.0f}%` {charging_emoji}"
        )
        
        # Enviamos la notificación de confirmación a Telegram
        await pyrogram_client.send_message(
            chat_id=user_id,
            text=message_text
        )
    except Exception as e:
        logger.error(f"No se pudo enviar el mensaje de confirmación a Telegram: {e}")
        
    return {"status": "ok"}


# --- 3. Arranque del Servidor ---

if __name__ == "__main__":
    logger.info("Iniciando servidor de prueba para Traccar...")
    uvicorn.run(api, host="0.0.0.0", port=9091)
