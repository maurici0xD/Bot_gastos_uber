# uber_bot_final.py
# Versión definitiva que implementa el plan maestro completo con todas las funcionalidades.

import os
import sqlite3
import math
import logging
import json
import asyncio
from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI, Body
from pydantic import BaseModel, Field
from staticmap import StaticMap, Line, CircleMarker
from datetime import datetime, timezone, timedelta
# Importamos el manejador de zonas horarias y definimos la de Bogotá
try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Fallback para versiones de Python más antiguas
    from pytz import timezone as ZoneInfo
    logging.warning("Módulo 'zoneinfo' no encontrado, usando 'pytz' como alternativa.")

BOGOTA_TZ = ZoneInfo("America/Bogota")
import openrouteservice
from openrouteservice.exceptions import ApiError

try:
    import polyline
except ImportError:
    polyline = None
    logging.warning("Librería 'polyline' no encontrada.")

from pyrogram import Client, filters, idle, StopPropagation, ContinuePropagation
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.raw.types import UpdateNewMessage, UpdateEditMessage, MessageMediaGeo, MessageMediaGeoLive
from pyrogram.errors import MessageNotModified, MessageIdInvalid
from pyrogram.raw import types as raw_types

# --- Logging ---
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)
logging.getLogger("pyrogram").setLevel(logging.WARNING)

# --- Claves y Constantes ---
try:
    API_ID = int(os.getenv("TELEGRAM_API_ID"))
    API_HASH = os.getenv("TELEGRAM_API_HASH")
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    ORS_KEY = os.getenv("ORS_API_KEY", None)
    if not all([API_ID, API_HASH, TOKEN]):
         raise ValueError("TELEGRAM_API_ID, TELEGRAM_API_HASH y TELEGRAM_BOT_TOKEN son requeridos.")
except (ValueError, TypeError) as e:
    logger.critical(f"Error en variables de entorno: {e}"); exit()

# --- Clientes y Configuración ---
app = Client("uber_bot_final_session", api_id=API_ID, api_hash=API_HASH, bot_token=TOKEN)

# Cliente para la API PÚBLICA (solo para obtener nombres de lugares)
ors_client_public_api = openrouteservice.Client(key=ORS_KEY) if ORS_KEY else None
if not ors_client_public_api:
    logger.warning("ORS_API_KEY no encontrada. No se podrán obtener nombres de lugares.")

# Cliente para nuestro servidor LOCAL (para rutas y auditorías ilimitadas)
ors_client = openrouteservice.Client(base_url='http://mauinternetservice.ddns.net:8080/ors')

sessions = {}
conn = sqlite3.connect("uber_data.db", check_same_thread=False)
cursor = conn.cursor()

KM_PER_GALLON = 40.0
PRICE_PER_GALLON = 16182.0
TOLLS = [
    {"name": "Túnel de Oriente - Seminario", "lat": 6.2229518, "lon": -75.5343687, "cost": 25000, "type": "tunel_oriente", "direction": "subida"},
    {"name": "Túnel de Oriente - Sajonia", "lat": 6.1790659, "lon": -75.4527765, "cost": 25000, "type": "tunel_oriente", "direction": "bajada"},
    {"name": "Variante Palmas", "lat": 6.170771, "lon": -75.4786281, "cost": 17800, "type": "normal"},
    {"name": "Peaje Copacabana", "lat": 6.3278548, "lon": -75.5156628, "cost": 17800, "type": "normal"},
]
STATUS_UPDATE_INTERVAL = 5
WATCHDOG_TIMEOUT_SECONDS = 300
WATCHDOG_SNOOZE_MINUTES = 30
MESSAGE_LIFETIME_HOURS = 12

# --- NUEVAS CONSTANTES PARA EL CÁLCULO DE DISTANCIA ---
DETOUR_RATIO_THRESHOLD = 4.0 
STOP_DETECTION_METERS = 15.0

# --- Gestión de Base de Datos ---
def setup_database():
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS active_sessions (
        user_id INTEGER PRIMARY KEY,
        session_data_json TEXT
    )""")
    cursor.execute("CREATE TABLE IF NOT EXISTS message_log (message_id INTEGER, chat_id INTEGER, expires_at TEXT NOT NULL, PRIMARY KEY (message_id, chat_id))")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        vehicle_model TEXT,
        fuel_type TEXT,
        price_per_unit REAL,
        km_per_unit REAL
    )""")
    conn.commit()
    logger.info("Base de datos configurada.")

def load_sessions_from_db():
    cursor.execute("SELECT user_id, session_data_json FROM active_sessions")
    for row in cursor.fetchall():
        uid, session_data_json = row
        try:
            session_data = json.loads(session_data_json)
            # Reconvertir los strings de fecha a objetos datetime
            for key, value in session_data.items():
                if (key.endswith('_ts') or key.endswith('_until')) and value:
                    session_data[key] = datetime.fromisoformat(value)
            
            # Asignar los datos cargados a la sesión en memoria
            sessions[uid] = session_data
            sessions[uid]['status_task'] = None # Las tareas nunca se guardan, siempre se recrean
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"Error al cargar sesión corrupta para el usuario {uid}: {e}")
            delete_session_from_db(uid) # Borrar la sesión corrupta
    if sessions: logger.info(f"Cargadas {len(sessions)} sesiones activas desde DB.")

def save_session_to_db(uid):
    s = sessions.get(uid)
    if not s: return
    
    # Crear una copia para no modificar el objeto en memoria
    data_to_save = s.copy()
    data_to_save.pop('status_task', None) # No se puede guardar la tarea de asyncio
    
    # Convertir todos los objetos datetime a strings en formato ISO
    for key, value in data_to_save.items():
        if isinstance(value, datetime):
            data_to_save[key] = value.isoformat()
            
    # Guardar el diccionario completo como un único string JSON
    cursor.execute("INSERT OR REPLACE INTO active_sessions (user_id, session_data_json) VALUES (?, ?)", 
                   (uid, json.dumps(data_to_save)))
    conn.commit()
    logger.debug(f"Sesión para {uid} guardada en la base de datos.")

def delete_session_from_db(uid):
    cursor.execute("DELETE FROM active_sessions WHERE user_id = ?", (uid,)); conn.commit()

def get_user(user_id):
    """Obtiene los datos de un usuario de la base de datos."""
    cursor.execute("SELECT user_id, name, vehicle_model, fuel_type, price_per_unit, km_per_unit FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        return None
    # Convierte la tupla a un diccionario para fácil acceso
    user_data = {
        "user_id": row[0],
        "name": row[1],
        "vehicle_model": row[2],
        "fuel_type": row[3],
        "price_per_unit": row[4],
        "km_per_unit": row[5]
    }
    return user_data

def update_user_data(user_id, key, value):
    """Actualiza un campo específico de un usuario en la sesión y DB."""
    # Primero actualiza el diccionario en la sesión
    if 'registration_data' not in sessions[user_id]:
        sessions[user_id]['registration_data'] = {}
    sessions[user_id]['registration_data'][key] = value
    
    # Inserta o actualiza el registro completo en la DB
    data = sessions[user_id]['registration_data']
    cursor.execute("""
        INSERT INTO users (user_id, name, vehicle_model, fuel_type, price_per_unit, km_per_unit)
        VALUES (:user_id, :name, :vehicle_model, :fuel_type, :price_per_unit, :km_per_unit)
        ON CONFLICT(user_id) DO UPDATE SET
            name = excluded.name,
            vehicle_model = excluded.vehicle_model,
            fuel_type = excluded.fuel_type,
            price_per_unit = excluded.price_per_unit,
            km_per_unit = excluded.km_per_unit
    """, {
        'user_id': user_id,
        'name': data.get('name'),
        'vehicle_model': data.get('vehicle_model'),
        'fuel_type': data.get('fuel_type'),
        'price_per_unit': data.get('price_per_unit'),
        'km_per_unit': data.get('km_per_unit')
    })
    conn.commit()

# --- Lógica de Mensajes y Auxiliares ---
def log_message(message: Message, hours=0.05):
    expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)
    cursor.execute("INSERT OR REPLACE INTO message_log VALUES (?, ?, ?)", (message.id, message.chat.id, expires_at.isoformat())); conn.commit()

def keep_message_alive(mid, cid, hours=MESSAGE_LIFETIME_HOURS):
    expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)
    cursor.execute("UPDATE message_log SET expires_at = ? WHERE message_id = ? AND chat_id = ?", (expires_at.isoformat(), mid, cid)); conn.commit()

def now_iso(): return datetime.now(timezone.utc).isoformat()
def get_location_name(coords):
    # Usamos el cliente de la API pública, que es el que tiene el servicio Pelias
    if not ors_client_public_api or not coords: return "Ubicación desconocida"
    try:
        # El método correcto es pelias_reverse, que está disponible en el cliente
        result = ors_client_public_api.pelias_reverse(point=(coords[1], coords[0]), size=1)
        return result['features'][0]['properties'].get('label', "Ubicación desconocida")
    except Exception as e:
        logger.warning(f"No se pudo obtener el nombre de la ubicación: {e}")
        return "Ubicación desconocida"

def haversine(p1, p2):
    R=6371;dLat,dLon=map(math.radians,[p2[0]-p1[0],p2[1]-p1[1]]);a=math.sin(dLat/2)**2+math.cos(math.radians(p1[0]))*math.cos(math.radians(p2[0]))*math.sin(dLon/2)**2;return R*2*math.atan2(math.sqrt(a),math.sqrt(1-a))

def line_intersects(p1, p2, p3, p4):
    def o(p,q,r): v=(q[1]-p[1])*(r[0]-q[0])-(q[0]-p[0])*(r[1]-q[1]); return 0 if v==0 else 1 if v>0 else 2
    o1,o2,o3,o4=o(p1,p2,p3),o(p1,p2,p4),o(p3,p4,p1),o(p3,p4,p2); return o1!=o2 and o3!=o4

def format_duration(d):
    s = d.total_seconds(); h = int(s // 3600); m = int((s % 3600) // 60)
    return f"{h}h {m}m" if h > 0 else f"{m}m"

def summarize_main_roads(route_data, limit=5):
    if not route_data or not route_data.get('features'):
        return "No disponible"
    try:
        street_distances = {}
        # Bucle principal que recorre TODOS los segmentos de la ruta
        for segment in route_data['features'][0]['properties']['segments']:
            # Por cada segmento, recorremos todos sus pasos (calles)
            for step in segment.get('steps', []):
                street_name = step.get('name', 'Vía sin nombre').strip()
                if street_name and street_name != '-':
                    street_distances[street_name] = street_distances.get(street_name, 0) + step['distance']
        
        if not street_distances:
            return "Ruta no detallada."
        
        # El resto de la lógica para ordenar y formatear se mantiene igual
        sorted_streets = sorted(street_distances.items(), key=lambda item: item[1], reverse=True)
        main_streets = [f"• {name} ({dist/1000:.1f} km)" for name, dist in sorted_streets[:limit]]
        return "\n".join(main_streets)
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"Error al parsear resumen de vías: {e}")
        return "Error al analizar ruta."
    
async def get_real_route_from_ors(coords):
    if not ors_client or len(coords) < 2: return None, "ORS no configurado o ruta muy corta."
    try:
        # Pedimos explícitamente la geometría para la auditoría de peajes
        route = await asyncio.to_thread(
            ors_client.directions,
            coordinates=[[lon, lat] for lat, lon in coords],
            radiuses=[-1] * len(coords),
            geometry='true',
            instructions=True,
            format='geojson'
        )
        return route, None
    except Exception as e:
        logger.error(f"Error en API de ORS al obtener ruta completa: {e}")
        return None, str(e)

# --- Modelos de Datos para la API de Traccar ---
class Coords(BaseModel):
    latitude: float
    longitude: float
    speed: float

class Battery(BaseModel):
    level: float
    is_charging: bool = Field(..., alias='is_charging')

class Location(BaseModel):
    coords: Coords
    timestamp: datetime
    battery: Battery

class TraccarPayload(BaseModel):
    location: Location
    device_id: str = Field(..., alias='device_id')

# --- Lógica de Negocio ---
async def check_toll_crossing(client, uid, old_point, new_point):
    session = sessions.get(uid)
    if not session: return

    for toll in TOLLS:
        toll_name = toll['name']
        if toll_name in [t['name'] for t in session.get('crossed_tolls_this_segment', [])]:
            continue

        toll_lat, toll_lon, toll_cost = toll['lat'], toll['lon'], toll['cost']
        offset = 0.001
        toll_p1, toll_p2 = (toll_lat - offset, toll_lon), (toll_lat + offset, toll_lon)

        if line_intersects(old_point, new_point, toll_p1, toll_p2):
            # Lógica Direccional para el Túnel de Oriente
            if toll.get('type') == 'tunel_oriente':
                travel_direction = "subida" if new_point[1] > old_point[1] else "bajada"
                
                if travel_direction != toll.get('direction') or travel_direction == session.get('last_tunel_direction_paid'):
                    logger.info(f"Cruce de peaje direccional '{toll_name}' ignorado.")
                    continue
                
                session['last_tunel_direction_paid'] = travel_direction

            # Si llegamos aquí, el peaje es válido
            logger.info(f"Peaje '{toll_name}' válido y cruzado por el usuario {uid}")
            toll_msg = await client.send_message(uid, f"🔔 Peaje detectado: **{toll_name}** | Costo: `COP ${toll_cost:,.0f}`")
            log_message(toll_msg)

            toll_info = {"name": toll_name, "cost": toll_cost, "timestamp": now_iso()}
            session['crossed_tolls_this_segment'].append(toll_info)
            if session['current_state'] == 'en_viaje_pago':
                session['current_trip']['toll_cost'] += toll_cost
            elif session['current_state'] == 'en_desplazamiento':
                session['current_displacement']['toll_cost'] += toll_cost
            
            save_session_to_db(uid)
            break

def get_keyboard_for_state(state):
    debug_button = InlineKeyboardButton("🗺️ Ver Mapa de Ruta", callback_data="debug_generate_map")
    if state == "en_desplazamiento":
        return InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Iniciar Viaje con Pasajero", callback_data="start_paid_trip")],[debug_button],[InlineKeyboardButton("❌ Finalizar Jornada", callback_data="cancel_session")]])
    elif state == "en_viaje_pago":
        return InlineKeyboardMarkup([[InlineKeyboardButton("🏁 Finalizar Viaje con Pasajero", callback_data="finish_trip")],[debug_button],[InlineKeyboardButton("❌ Finalizar Jornada", callback_data="cancel_session")]])
    elif state == "pending_income":
        return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar Jornada", callback_data="cancel_session")]])
    return None

def generate_status_text(s, route_data=None):
    duration = datetime.now(timezone.utc) - s['start_ts']
    last_update = f"hace {int((datetime.now(timezone.utc) - s['last_update_ts']).total_seconds())}s" if s.get('last_update_ts') else "Esperando GPS..."
    header = f"📊 **Jornada de Trabajo**\n🕒 Duración Total: `{format_duration(duration)}` | 🛰️ GPS: `{last_update}`"

    # --- Construcción del Cuerpo del Mensaje ---
    body = ""
    if s['current_state'] == 'en_viaje_pago':
        trip = s['current_trip']
        #start_time = s['displacement_paths'][-1][-1]['timestamp'] if s['displacement_paths'] and s['displacement_paths'][-1] else s['start_ts'].isoformat()
        #trip_duration = format_duration(datetime.now(timezone.utc) - datetime.fromisoformat(start_time))
        start_ts_del_viaje = trip.get('start_ts', now_iso()) # Obtenemos el timestamp del viaje
        trip_duration = format_duration(datetime.now(timezone.utc) - datetime.fromisoformat(start_ts_del_viaje))
        dist, fuel_cost, toll_cost = trip.get('distance', 0), trip.get('fuel_cost', 0), trip.get('toll_cost', 0)

        tolls_list_str = [f"   • {t['name']} ({datetime.fromisoformat(t['timestamp']).astimezone(BOGOTA_TZ).strftime('%H:%M')})" for t in s.get('crossed_tolls_this_segment', [])]
        tolls_str_body = "\n" + "\n".join(tolls_list_str) if tolls_list_str else " Ninguno"

        main_roads = summarize_main_roads(route_data)

        body = (f"🚗 **VIAJE CON PASAJERO EN CURSO**\n"
                f"📍 De: `{trip.get('start_location_name', '...')}`\n"
                f"⏱️ Tiempo Viaje: `{trip_duration}`\n"
                f"🛣️ Distancia Viaje: `{dist:.2f} km` (Real)\n"
                f"--- Costos del Viaje Actual ---\n"
                f"⛽ Gasolina: `COP ${fuel_cost:,.0f}`\n"
                f"톨 Peajes: `COP ${toll_cost:,.0f}`{tolls_str_body}\n"
                f"**Subtotal Viaje: `COP ${fuel_cost + toll_cost:,.0f}`**\n"
                f"--- Vías Principales ---\n{main_roads}")

    elif s['current_state'] == 'en_desplazamiento':
        disp = s['current_displacement']
        dist, fuel_cost, toll_cost = disp.get('distance', 0), disp.get('fuel_cost', 0), disp.get('toll_cost', 0)
        start_time = datetime.fromisoformat(disp['start_ts'])
        disp_duration = format_duration(datetime.now(timezone.utc) - start_time)

        tolls_list_str = [f"   • {t['name']} ({datetime.fromisoformat(t['timestamp']).astimezone(BOGOTA_TZ).strftime('%H:%M')})" for t in s.get('crossed_tolls_this_segment', [])]
        tolls_str_body = "\n         " + "\n         ".join(tolls_list_str) if tolls_list_str else " Ninguno"

        body = (f"🔄 **EN DESPLAZAMIENTO**\n"
                f"⏱️ Tiempo de este Tramo: `{disp_duration}`\n"
                f"🛣️ Distancia de este Tramo: `{dist:.2f} km` (Real)\n"
                f"--- Costos de este Tramo ---\n"
                f"⛽ Gasolina: `COP ${fuel_cost:,.0f}`\n"
                f"톨 Peajes: `COP ${toll_cost:,.0f}`{tolls_str_body}\n"
                f"**Subtotal Tramo: `COP ${fuel_cost + toll_cost:,.0f}`**")

    elif s['current_state'] == 'pending_income':
        # El mensaje detallado para este estado se genera en el callback_handler
        body = "💰 **Esperando Ingreso**\nPor favor, responde con el monto recibido por el último viaje."

    # --- Footer Financiero ---
    total_income = sum(t.get('income', 0) for t in s.get('completed_trips', []))
    paid_trips_fuel_cost = sum(t.get('fuel_cost', 0) for t in s.get('completed_trips', []))
    paid_trips_toll_cost = sum(t.get('toll_cost', 0) for t in s.get('completed_trips', []))
    total_paid_trips_cost = paid_trips_fuel_cost + paid_trips_toll_cost

    disp_fuel_cost_total = s['session_totals'].get('total_displacement_fuel_cost', 0)
    disp_toll_cost_total = s['session_totals'].get('total_displacement_toll_cost', 0)

    # Incluimos los costos del tramo de desplazamiento actual en el total en tiempo real
    if s['current_state'] == 'en_desplazamiento':
        disp_fuel_cost_total += s['current_displacement'].get('fuel_cost', 0)
        disp_toll_cost_total += s['current_displacement'].get('toll_cost', 0)

    total_disp_cost = disp_fuel_cost_total + disp_toll_cost_total

    disp_tolls_list_footer = []
    for toll in s['session_totals'].get('total_displacement_tolls_list', []):
         disp_tolls_list_footer.append(f"• {toll['name']} ({datetime.fromisoformat(toll['timestamp']).astimezone(BOGOTA_TZ).strftime('%H:%M')})")
    # Añadimos los peajes del tramo actual si estamos en desplazamiento
    if s['current_state'] == 'en_desplazamiento':
        for toll in s.get('crossed_tolls_this_segment', []):
            disp_tolls_list_footer.append(f"• {toll['name']} ({datetime.fromisoformat(toll['timestamp']).astimezone(BOGOTA_TZ).strftime('%H:%M')})")

    disp_tolls_footer_str = "\n         ".join(disp_tolls_list_footer) if disp_tolls_list_footer else "Ninguno"

    total_egresos = total_paid_trips_cost + total_disp_cost
    net_profit = total_income - total_egresos

    footer = (f"💰 **RESUMEN FINANCIERO DE JORNADA**\n"
              f"✅ Ingresos Totales ({len(s['completed_trips'])} Viajes): `COP ${total_income:,.0f}`\n"
              f"--- Egresos (Costos Acumulados) ---\n"
              f"   • Costos en Viajes Pagos: `COP ${total_paid_trips_cost:,.0f}`\n"
              f"   • Costos en Desplazamiento: `COP ${total_disp_cost:,.0f}`\n"
              f"      - ⛽ Gasolina: `COP ${disp_fuel_cost_total:,.0f}`\n"
              f"      - 톨 Peajes: `COP ${disp_toll_cost_total:,.0f}`\n"
              f"         {disp_tolls_footer_str}\n"
              f"**Total Egresos: `COP ${total_egresos:,.0f}`**\n"
              f"----------------------------------\n"
              f"💵 **GANANCIA NETA REAL: `COP ${net_profit:,.0f}`**")

    return f"{header}\n──────────────────\n{body}\n──────────────────\n{footer}"

def generate_final_summary(s):
    """Genera un texto de resumen detallado al finalizar una jornada."""
    
    # 1. Totales de viajes completados (con pasajero)
    completed_trips = s.get('completed_trips', [])
    total_income = sum(t.get('income', 0) for t in completed_trips)
    paid_trips_fuel_cost = sum(t.get('fuel_cost', 0) for t in completed_trips)
    paid_trips_toll_cost = sum(t.get('toll_cost', 0) for t in completed_trips)

    # 2. Totales de desplazamientos (sin pasajero)
    disp_fuel_cost = s['session_totals'].get('total_displacement_fuel_cost', 0)
    disp_toll_cost = s['session_totals'].get('total_displacement_toll_cost', 0)

    # 3. ¡Importante! Añadir costos del último tramo de desplazamiento si estaba activo
    if s.get('current_state') == 'en_desplazamiento' and s.get('current_displacement'):
        last_disp = s['current_displacement']
        disp_fuel_cost += last_disp.get('fuel_cost', 0)
        disp_toll_cost += last_disp.get('toll_cost', 0)

    # 4. Calcular totales generales
    total_fuel_cost = paid_trips_fuel_cost + disp_fuel_cost
    total_toll_cost = paid_trips_toll_cost + disp_toll_cost
    total_expenses = total_fuel_cost + total_toll_cost
    net_profit = total_income - total_expenses

    # 5. Calcular duración total
    start_ts = s.get('start_ts')
    if isinstance(start_ts, str): # Si viene de la DB
        start_ts = datetime.fromisoformat(start_ts)
    total_duration = format_duration(datetime.now(timezone.utc) - start_ts)

    # Peajes de viajes con pasajero
    paid_tolls_details = []
    for trip in completed_trips:
        for toll in trip.get('tolls_crossed', []):
            paid_tolls_details.append(
                f"         • {toll['name']} ({datetime.fromisoformat(toll['timestamp']).astimezone(BOGOTA_TZ).strftime('%H:%M')})"
            )
    paid_tolls_section = "\n" + "\n".join(paid_tolls_details) if paid_tolls_details else ""

    # Peajes de desplazamientos
    disp_tolls_details = []
    # Primero, los históricos
    for toll in s['session_totals'].get('total_displacement_tolls_list', []):
        disp_tolls_details.append(
            f"         • {toll['name']} ({datetime.fromisoformat(toll['timestamp']).astimezone(BOGOTA_TZ).strftime('%H:%M')})"
        )
    # Segundo, los del último tramo activo
    if s.get('current_state') == 'en_desplazamiento':
        for toll in s.get('crossed_tolls_this_segment', []):
            disp_tolls_details.append(
                f"         • {toll['name']} ({datetime.fromisoformat(toll['timestamp']).astimezone(BOGOTA_TZ).strftime('%H:%M')})"
            )
    disp_tolls_section = "\n" + "\n".join(disp_tolls_details) if disp_tolls_details else ""

    # --- 3. Construir el texto del resumen final (actualizado) ---
    summary_text = (
        f"🏁 **Resumen de la Jornada Finalizada** 🏁\n"
        f"🕒 Duración Total: `{total_duration}`\n"
        f"──────────────────\n"
        f"💰 **INGRESOS**\n"
        f"✅ Ingresos Brutos ({len(completed_trips)} viajes): `COP ${total_income:,.0f}`\n"
        f"──────────────────\n"
        f"💸 **EGRESOS (GASTOS TOTALES): `COP ${total_expenses:,.0f}`**\n\n"
        f" *Breakdown de Gastos:*\n"
        f"   • **⛽ Gasolina (Total):** `COP ${total_fuel_cost:,.0f}`\n"
        f"      - En viajes pagos: `COP ${paid_trips_fuel_cost:,.0f}`\n"
        f"      - En desplazamiento: `COP ${disp_fuel_cost:,.0f}`\n\n"
        f"   • **톨 Peajes (Total):** `COP ${total_toll_cost:,.0f}`\n"
        f"      - En viajes pagos: `COP ${paid_trips_toll_cost:,.0f}`{paid_tolls_section}\n"
        f"      - En desplazamiento: `COP ${disp_toll_cost:,.0f}`{disp_tolls_section}\n"
        f"──────────────────\n"
        f"💵 **GANANCIA NETA FINAL: `COP ${net_profit:,.0f}`**"
    )
    return summary_text

async def generate_and_send_route_map(client, uid, session):
    """
    Genera un mapa de alta resolución con marcadores de INICIO (verde) y FIN (rojo)
    para visualizar el recorrido.
    """
    path_to_debug = []
    state = session.get('current_state')
    if state == 'en_viaje_pago':
        path_to_debug = session.get('current_trip', {}).get('path', [])
    elif state == 'en_desplazamiento':
        path_to_debug = session.get('current_displacement', {}).get('path', [])

    if len(path_to_debug) < 2:
        await client.send_message(uid, "❌ No hay suficientes puntos en la ruta actual para generar un mapa.")
        return

    route_data, error_msg = await get_real_route_from_ors(path_to_debug)
    if error_msg:
        await client.send_message(uid, f"⚠️ No se pudo obtener la ruta desde ORS: {error_msg}")
        return
        
    logger.debug(f"Respuesta de ORS para la ruta: {route_data}")

    # Usamos la resolución preferida
    m = StaticMap(width=1600, height=1200)

    try:
        route_coordinates = route_data['features'][0]['geometry']['coordinates']
        line = Line(route_coordinates, color="red", width=3)
        m.add_line(line)
    except (KeyError, IndexError):
        logger.warning("No se pudo dibujar la línea de la ruta en la imagen estática.")

    # Lógica simplificada para marcadores de Inicio, Fin e Intermedios
    total_points = len(path_to_debug)
    for i, point in enumerate(path_to_debug):
        lon, lat = point[1], point[0]
        
        if i == 0:
            # El primer punto es VERDE
            marker = CircleMarker((lon, lat), '#00FF00', 10) # Verde, radio 10px
            m.add_marker(marker)
        elif i == total_points - 1:
            # El último punto es ROJO
            marker = CircleMarker((lon, lat), '#FF0000', 10) # Rojo, radio 10px
            m.add_marker(marker)
        else:
            # Para no saturar, dibujamos solo 1 de cada 5 puntos intermedios
            if i % 5 == 0:
                marker = CircleMarker((lon, lat), 'blue', 5) # Azul, radio 5px
                m.add_marker(marker)
        
    image = m.render() 
    image_path = "debug_map.png"
    image.save(image_path)

    msg = await client.send_photo(
        chat_id=uid,
        photo=image_path,
        caption=(
            "✅ **Mapa de Ruta Generado.**\n\n"
            "🟢 **Punto de Inicio**\n"
            "🔴 **Punto Final**\n"
            "🔵 Puntos intermedios\n\n"
            "Si el punto verde y el rojo están muy juntos, es un recorrido de ida y vuelta."
        )
    )
    log_message(msg)
    if os.path.exists(image_path):
        os.remove(image_path)

async def send_gps_warning(client, uid):
    session = sessions.get(uid)
    if not session: return
    
    snoozed_until = session.get('watchdog_snoozed_until')
    if snoozed_until and datetime.now(timezone.utc) < snoozed_until: return

    logger.warning(f"Señal GPS perdida para el usuario {uid}")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"🤫 Pausar Alertas por {WATCHDOG_SNOOZE_MINUTES} min", callback_data="snooze_watchdog")]])
    msg = await client.send_message(uid, "🛰️ **Alerta:** He perdido tu señal GPS por más de 5 minutos. Si estás en un túnel, puedes pausar esta alerta.", reply_markup=keyboard)
    log_message(msg)
    session['watchdog_snoozed_until'] = datetime.now(timezone.utc) + timedelta(minutes=5)
    save_session_to_db(uid)

async def status_updater_task(uid, client):
    logger.info(f"Iniciando tarea de actualización de estado para {uid}.")
    while uid in sessions:
        try:
            session = sessions.get(uid)
            if not session or session.get('current_state') == "pending_income": break
            
            if session.get('last_update_ts'):
                if (datetime.now(timezone.utc) - session['last_update_ts']).total_seconds() > WATCHDOG_TIMEOUT_SECONDS:
                    await send_gps_warning(client, uid)
            
            # El cálculo de distancia ya no se hace aquí.
            # Pasamos None a route_data para evitar una llamada innecesaria a ORS.
            text = generate_status_text(session, route_data=None)
            mid = session.get('status_message_id')
            keyboard = get_keyboard_for_state(session['current_state'])
            
            if mid:
                await client.edit_message_text(uid, mid, text, reply_markup=keyboard)
                keep_message_alive(mid, uid, hours=MESSAGE_LIFETIME_HOURS)
        except MessageNotModified: pass
        except MessageIdInvalid:
            logger.warning(f"MessageIdInvalid para {mid}. Recreando panel."); await reset_status_message(client, uid); break
        except Exception as e:
            logger.error(f"Error en bucle de actualización para {uid}", exc_info=True)
        await asyncio.sleep(STATUS_UPDATE_INTERVAL)
            
    logger.info(f"Tarea de actualización de estado para {uid} finalizada.")
    if sessions.get(uid) and sessions[uid].get('status_task'):
        try: sessions[uid]['status_task'].cancel()
        except: pass
        sessions[uid]['status_task'] = None

async def reset_status_message(client, uid):
    session = sessions.get(uid)
    if not session: return

    if session.get('status_task'):
        try: session['status_task'].cancel()
        except: pass
        session['status_task'] = None
    
    old_mid = session.get('status_message_id')
    if old_mid:
        try: await client.delete_messages(uid, old_mid)
        except Exception: pass

    text = generate_status_text(session)
    keyboard = get_keyboard_for_state(session.get('current_state'))
    try:
        new_msg = await client.send_message(uid, text, reply_markup=keyboard)
        log_message(new_msg, hours=MESSAGE_LIFETIME_HOURS)
        session['status_message_id'] = new_msg.id
        
        if session.get('current_state') != 'pending_income':
            task = asyncio.create_task(status_updater_task(uid, client))
            session['status_task'] = task
        save_session_to_db(uid)
    except Exception as e:
        logger.error(f"No se pudo crear el nuevo panel de estado para {uid}: {e}")

async def cleanup_task(client):
    while True:
        await asyncio.sleep(60)
        try:
            expired_tuples = cursor.execute("SELECT message_id, chat_id FROM message_log WHERE expires_at < ?", (now_iso(),)).fetchall()
            if not expired_tuples: continue
            
            expired_by_chat = {}
            for mid, cid in expired_tuples:
                if cid not in expired_by_chat: expired_by_chat[cid] = []
                expired_by_chat[cid].append(mid)

            for cid, mids in expired_by_chat.items():
                try:
                    await client.delete_messages(cid, mids)
                    logger.info(f"Limpiados {len(mids)} mensajes en el chat {cid}.")
                except Exception as e:
                    logger.warning(f"No se pudieron borrar mensajes en el chat {cid}: {e}")
                finally:
                    cursor.executemany("DELETE FROM message_log WHERE message_id=? AND chat_id=?", [(mid, cid) for mid in mids])
                    conn.commit()
        except Exception as e:
            logger.error(f"Error en tarea de limpieza: {e}")

# --- Lógica de Acciones ---
async def get_real_route_from_ors(coords):
    if not ors_client or len(coords) < 2: return None, "ORS no configurado o ruta muy corta."
    try:
        # Pedimos explícitamente la geometría
        route = await asyncio.to_thread(
            ors_client.directions,
            coordinates=[[lon, lat] for lat, lon in coords],
            radiuses=[-1] * len(coords),
            geometry='true',
            format='geojson'
        )
        return route, None
    except Exception as e:
        logger.error(f"Error en API de ORS al obtener ruta completa: {e}")
        return None, str(e)
    


# --- Handlers ---

@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message: Message):
    uid = message.from_user.id
    user_data = get_user(uid)

    if user_data:
        # El usuario ya está registrado
        if uid in sessions:
            # Tiene una sesión activa, que no haga nada para no interrumpir
            msg = await message.reply("Ya tienes una jornada de trabajo activa. Usa los botones del panel de estado.")
            log_message(msg)
        else:
            # No tiene sesión activa, lo saludamos y mostramos el menú principal
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Iniciar Nueva Jornada", callback_data="info_start_trip")],
                [InlineKeyboardButton("✍️ Editar datos de mi vehículo", callback_data="edit_vehicle_data")]
            ])
            await message.reply(
                f"¡Hola de nuevo, {user_data['name']}! 👋\n\n¿Qué deseas hacer hoy?",
                reply_markup=keyboard
            )
    else:
        # Usuario nuevo, iniciar proceso de registro
        sessions[uid] = {'conversation_state': 'awaiting_name'}
        msg = await message.reply(
            "¡Bienvenido! Soy tu asistente de viajes. 😊\n\n"
            "Para empezar, necesito registrar algunos datos. Por favor, dime tu nombre."
        )
        log_message(msg)
    await message.delete()

async def setup_cmd(client, message: Message):
    """NUEVO: Envía las instrucciones para configurar la app Traccar Client."""
    user_id = message.from_user.id
    # El puerto 9091 es el que hemos usado en las pruebas
    server_url = "http://mauinternetservice.ddns.net:9091/api/traccar"
    instructions = (
        "**Configuración de Traccar Client para el Bot**\n\n"
        "1. **Descarga la App:**\n"
        "   - [Android: Traccar Client](https://play.google.com/store/apps/details?id=org.traccar.client)\n"
        "   - [iOS: Traccar Client](https://apps.apple.com/us/app/traccar-client/id843156974)\n\n"
        "2. **Configura la App con estos datos exactos:**\n"
        f"   - **Identificador del dispositivo:** `{user_id}`\n"
        f"   - **URL del servidor:** `{server_url}`\n"
        "   - **Frecuencia:** `10` (segundos)\n"
        "   - **Distancia:** `0`\n"
        "   - **Ángulo:** `0`\n\n"
        "3. **Activa el 'Estado del servicio'** en la app."
    )
    await message.reply(instructions, disable_web_page_preview=True)

# Reemplaza esta función completa en tu script
@app.on_message(filters.text & filters.private)
async def text_handler(client, message: Message):
    uid = message.from_user.id
    text = message.text.strip()
    session = sessions.get(uid)
    await message.delete()

    if not session:
        # Si no hay sesión, podría ser un usuario registrado pero inactivo
        user_data = get_user(uid)
        if user_data:
            await start_cmd(client, message) # Reutilizamos el comando start para mostrarle el menú
        else: # O un usuario completamente nuevo que no ha empezado el registro
             msg = await message.reply("Por favor, envía /start para comenzar.")
             log_message(msg)
        return

    state = session.get('conversation_state')

    # --- FLUJO DE REGISTRO ---
    if state == 'awaiting_name':
        update_user_data(uid, 'name', text)
        session['conversation_state'] = 'awaiting_vehicle_model'
        msg = await message.reply("¡Gracias! Ahora, por favor, dime el modelo de tu vehículo (Ej: Renault Kwid).")
        log_message(msg)
    
    elif state == 'awaiting_vehicle_model':
        update_user_data(uid, 'vehicle_model', text)
        session['conversation_state'] = 'awaiting_fuel_type'
        msg = await message.reply("Entendido. ¿Qué tipo de combustible usa tu vehículo? (Ej: Gasolina Corriente, Diesel, Eléctrico)")
        log_message(msg)

    elif state == 'awaiting_fuel_type':
        update_user_data(uid, 'fuel_type', text)
        session['conversation_state'] = 'awaiting_fuel_price'
        msg = await message.reply("Perfecto. Ahora, dime el precio por unidad de ese combustible (Ej: 16182 por galón, 1200 por kWh). Envía solo el número.")
        log_message(msg)

    elif state == 'awaiting_fuel_price':
        try:
            price = float(text.replace(",", "").replace(".", ""))
            update_user_data(uid, 'price_per_unit', price)
            session['conversation_state'] = 'awaiting_consumption'
            msg = await message.reply(f"Registrado: ${price:,.0f}. Finalmente, dime el consumo promedio de tu vehículo (cuántos KM recorre por unidad de combustible, Ej: 40 km por galón).")
            log_message(msg)
        except ValueError:
            msg = await message.reply("❗ Valor inválido. Por favor, envía solo el número del precio.")
            log_message(msg)

    elif state == 'awaiting_consumption':
        try:
            consumption = float(text.replace(",", "").replace(".", ""))
            update_user_data(uid, 'km_per_unit', consumption)
            session.pop('conversation_state', None) # Terminamos el modo conversación
            session.pop('registration_data', None)
            
            msg = await message.reply(
                "¡Excelente! 🎉 Todos tus datos han sido guardados.\n\n"
                "Ahora ya puedes iniciar tu jornada. Simplemente **envía tu ubicación en tiempo real**."
            )
            log_message(msg, hours=1) # <--- AÑADIR (le damos 1 hora a este mensaje)
        except ValueError:
            msg = await message.reply("❗ Valor inválido. Por favor, envía solo el número del consumo (km por unidad).")
            log_message(msg) 

    # --- FLUJO DE INGRESO DE VIAJE (lógica existente) ---
    elif session.get('current_state') == 'pending_income':
        try:
            income = float(text)
        except ValueError:
            msg = await message.reply("❗ Valor inválido. Envía solo el número.", quote=True)
            log_message(msg)
            return

        # --- INICIO DE LA LÓGICA CORREGIDA ---
        
        # 1. Finalizamos el viaje pendiente y lo movemos a completados
        pending_trip = session.pop('pending_trip')
        pending_trip['income'] = income
        session['completed_trips'].append(pending_trip)

        # 2. Cambiamos el estado para iniciar un nuevo tramo de desplazamiento
        session['current_state'] = 'en_desplazamiento'
        
        # 3. Obtenemos el último punto del viaje recién terminado para empezar el nuevo tramo sin "saltos"
        last_point = None
        if pending_trip.get('path'):
            # Nos aseguramos de que la coordenada esté en formato de lista [lat, lon]
            last_point = list(pending_trip['path'][-1])

        # 4. Creamos el nuevo tramo de desplazamiento ACTIVO
        session['current_displacement'] = {
            "path": [last_point] if last_point else [],
            "start_ts": now_iso(),
            "distance": 0.0,
            "fuel_cost": 0.0,
            "toll_cost": 0.0
        }
        
        # 5. Reseteamos los contadores del segmento para este nuevo tramo
        session['crossed_tolls_this_segment'] = []
        session['last_tunel_direction_paid'] = None
        # NO tocamos 'displacement_paths'. Esa lista solo se actualiza cuando un tramo de desplazamiento TERMINA.

        # --- FIN DE LA LÓGICA CORREGIDA ---

        net_profit = income - pending_trip['fuel_cost'] - pending_trip['toll_cost']
        msg = await message.reply(f"✅ ¡Viaje registrado!\nGanancia neta: **COP ${net_profit:,.0f}**")
        log_message(msg)

        await reset_status_message(client, uid)
        save_session_to_db(uid)
    else:
        # El usuario escribe algo sin estar en un flujo de conversación
        msg = await message.reply("Para interactuar, usa los botones o inicia una jornada enviando tu ubicación en tiempo real.")
        log_message(msg)

@app.on_callback_query()
async def callback_handler(client, cb: CallbackQuery):
    uid = cb.from_user.id
    data = cb.data
    session = sessions.get(uid)
    # -- Acciones que no necesitan una sesión de viaje activa --
    if data == "show_help":
        await cb.answer()
        msg = await cb.message.reply_photo(
            photo="https://i.imgur.com/gL9n323.png",
            caption="Para iniciar una sesión:\n1. Pulsa el ícono del clip (📎)\n2. Selecciona 'Ubicación'\n3. Elige **'Ubicación en tiempo real'**."
        )
        log_message(msg)
        return

# En la función callback_handler
    elif data == "info_start_trip":
        await cb.answer("Iniciando nueva jornada...")
        
        if uid in sessions:
            await stop_status_task(client, uid)
            sessions.pop(uid, None)
            delete_session_from_db(uid)

        now = datetime.now(timezone.utc)
        sessions[uid] = {
            'start_ts': now, 'current_state': 'en_desplazamiento', 'last_update_ts': None,
            'watchdog_snoozed_until': None, 'last_tunel_direction_paid': None,
            'status_message_id': None, 'status_task': None, 'completed_trips': [],
            'pending_trip': None, 'crossed_tolls_this_segment': [], 'current_trip': {},
            'is_stopped_notified': False,
            'current_displacement': { "path": [], "start_ts": now.isoformat(), "distance": 0.0, "fuel_cost": 0.0, "toll_cost": 0.0 },
            'session_totals': { "total_displacement_distance": 0.0, "total_displacement_fuel_cost": 0.0, "total_displacement_toll_cost": 0.0, "total_displacement_tolls_list": [] }
        }
        
        await cb.message.edit_text("✅ ¡Jornada iniciada!\n\nAsegúrate de que tu app **Traccar Client esté activa** y enviando datos. Si necesitas ayuda, envía /setup.")
        await reset_status_message(client, uid)
        save_session_to_db(uid)
        return
        
    if data == "edit_vehicle_data":
        await cb.answer("Editando vehículo...")
        if uid not in sessions:
            sessions[uid] = {}  # Crea una sesión temporal si no existe
        
        user_data = get_user(uid)
        sessions[uid]['registration_data'] = user_data if user_data else {}
        sessions[uid]['registration_data']['user_id'] = uid

        sessions[uid]['conversation_state'] = 'awaiting_vehicle_model'
        await cb.message.edit_text("✍️ **Editando Vehículo**\n\nPor favor, dime el nuevo modelo de tu vehículo (Ej: Chevrolet Onix).")
        return

    # -- A partir de aquí, todas las acciones requieren una sesión de viaje activa --
    if not session:
        await cb.answer("Tu sesión de trabajo ha expirado. Por favor, inicia una nueva.", show_alert=True)
        try:
            await cb.message.delete()
        except:
            pass
        return

    # Si llegamos aquí, tenemos una sesión. Ahora podemos contestar al callback.
    await cb.answer()

    if data == "start_paid_trip":
        await cb.answer("Iniciando viaje con pasajero...")
        # 1. Finalizar el tramo de desplazamiento actual y acumular sus costos y peajes
        last_disp = session.get('current_displacement', {})
        if last_disp.get('distance', 0) > 0:
            session['session_totals']['total_displacement_distance'] += last_disp.get('distance', 0)
            session['session_totals']['total_displacement_fuel_cost'] += last_disp.get('fuel_cost', 0)
            session['session_totals']['total_displacement_toll_cost'] += last_disp.get('toll_cost', 0)
            # Añadimos los peajes del tramo a la lista general de la jornada
            session['session_totals']['total_displacement_tolls_list'].extend(session.get('crossed_tolls_this_segment', []))

        # 2. Cambiar de estado e iniciar el viaje pago
        session['current_state'] = 'en_viaje_pago'
        last_point = None
        last_point = session['current_displacement']['path'][-1] if session.get('current_displacement', {}).get('path') else None
        
        start_name = get_location_name(last_point) if last_point else "Ubicación actual"
        
        session['current_trip'] = {
            'path': [last_point] if last_point else [], 
            'start_location_name': start_name,
            'start_ts': now_iso(), 
            'distance': 0.0, 'fuel_cost': 0.0, 'toll_cost': 0
        }
        # Reseteamos los contadores de segmento para el nuevo viaje
        session['crossed_tolls_this_segment'] = []
        session['last_tunel_direction_paid'] = None
        
        await reset_status_message(client, uid)
        save_session_to_db(uid)
        

    elif data == "finish_trip":
        trip = session.get('current_trip')
        if not trip or len(trip.get('path', [])) < 2:
            await cb.answer("Cancelando viaje sin ruta...", show_alert=True)
            
            # 1. Informar al usuario
            msg = await client.send_message(uid, "ℹ️ El viaje con pasajero fue cancelado sin haber iniciado ruta. Volviendo a modo 'desplazamiento'.")
            log_message(msg)

            # 2. Cambiar el estado
            session['current_state'] = 'en_desplazamiento'
            
            # 3. Iniciar un nuevo tramo de desplazamiento desde el último punto
            last_point = trip.get('path')[0] if trip and trip.get('path') else None
            session['current_displacement'] = {
                "path": [last_point] if last_point else [],
                "start_ts": now_iso(),
                "distance": 0.0,
                "fuel_cost": 0.0,
                "toll_cost": 0.0
            }

            # 4. Limpiar los datos del viaje cancelado
            session['current_trip'] = {}
            session['crossed_tolls_this_segment'] = []
            session['last_tunel_direction_paid'] = None

            # 5. Actualizar panel y guardar
            await reset_status_message(client, uid)
            save_session_to_db(uid)
            return # Detener la ejecución aquí

        await stop_status_task(client, uid)
        msg = await client.send_message(uid, "Generando resumen final del viaje... ⏳")
        log_message(msg)

        trip_path = trip.get('path', [])
        
        # Tomamos los valores que ya han sido calculados de forma incremental y precisa
        dist = trip.get('distance', 0)
        fuel_cost = trip.get('fuel_cost', 0)
        
        # Opcional: Aún llamamos a ORS, pero solo para obtener el resumen de vías principales
        route_data, _ = await get_real_route_from_ors(trip_path)

        tolls_crossed_list = session.get('crossed_tolls_this_segment', [])
        
        # --- INICIO DE LA AUDITORÍA DE PEAJES ---
        route_data, error_msg = await get_real_route_from_ors(trip_path)
        if route_data and polyline:
            try:
                geometry = route_data['features'][0]['geometry']['coordinates']
                decoded_path = [[lat, lon] for lon, lat in geometry]
                logger.info(f"Auditoría final de peajes para {uid} con {len(decoded_path)} puntos de ruta.")
                
                tolls_before_audit = list(session['crossed_tolls_this_segment'])
                
                # Hacemos una copia de la memoria de dirección para la auditoría
                audit_last_direction = session.get('last_tunel_direction_paid')

                for i in range(len(decoded_path) - 1):
                    await check_toll_crossing(client, uid, decoded_path[i], decoded_path[i+1])
                
                if len(session['crossed_tolls_this_segment']) > len(tolls_before_audit):
                    await client.send_message(uid, "ℹ️ Se detectaron peajes adicionales en el análisis final de la ruta (posiblemente por pérdida de señal GPS).")
            except Exception as e:
                logger.error(f"Error durante la auditoría de peajes: {e}")
                route_data = None # Si falla la auditoría, procedemos sin la ruta precisa
        # --- FIN DE LA AUDITORÍA DE PEAJES ---

        # --- INICIO DEL CÁLCULO DE COSTO DINÁMICO ---
        user_data = get_user(uid)
        if not user_data:
            logger.error(f"¡CRÍTICO! No se encontraron datos de usuario para {uid} al finalizar viaje.")
            km_per_unit = KM_PER_GALLON
            price_per_unit = PRICE_PER_GALLON
        else:
            km_per_unit = user_data.get('km_per_unit', KM_PER_GALLON)
            price_per_unit = user_data.get('price_per_unit', PRICE_PER_GALLON)
        
        if km_per_unit == 0: km_per_unit = 1 # Evitar división por cero

        # Usamos la distancia precisa de ORS si está disponible, si no, la aproximada
        dist = route_data['features'][0]['properties']['summary']['distance'] / 1000.0 if route_data else sum(haversine(p1, p2) for p1, p2 in zip(trip_path, trip_path[1:]))
        # ¡CÁLCULO DINÁMICO APLICADO!
        fuel_cost = (dist / km_per_unit) * price_per_unit
        # --- FIN DEL CÁLCULO DE COSTO DINÁMICO ---
        # Usamos el costo de peajes actualizado por la auditoría
        tolls_crossed_list = session.get('crossed_tolls_this_segment', [])
        toll_cost = sum(t['cost'] for t in tolls_crossed_list)

        toll_details_list = [
            f"   • {t['name']} ({datetime.fromisoformat(t['timestamp']).astimezone(BOGOTA_TZ).strftime('%H:%M')}) - `COP ${t['cost']:,}`"
            for t in tolls_crossed_list
        ]
        toll_details_str = "\n" + "\n".join(toll_details_list) if toll_details_list else " Ninguno"

        final_caption = (f"🏁 **Viaje Finalizado**\n"
                        f"Distancia (Precisa): `{dist:.2f} km`\n"
                        f"--- Desglose de Costos del Viaje ---\n"
                        f"⛽ Gasolina: `COP ${fuel_cost:,.0f}`\n"
                        f"톨 Peajes: `COP ${toll_cost:,.0f}`{toll_details_str}\n"
                        f"-----------------------------------\n"
                        f"**Costo Total del Viaje: `COP ${fuel_cost + toll_cost:,.0f}`**\n\n"
                        f"👇 **Escribe el monto recibido por el viaje.**")

        final_msg = await client.send_message(uid, final_caption)
        log_message(final_msg, hours=2)

        session['current_state'] = 'pending_income'
        session['pending_trip'] = {
            'path': trip_path,
            'distance': dist, 
            'fuel_cost': fuel_cost, 
            'toll_cost': toll_cost,
            'tolls_crossed': tolls_crossed_list
        }
        await reset_status_message(client, uid)
        save_session_to_db(uid)

    elif data == "cancel_session":
        current_state = session.get('current_state')

        if current_state == 'en_viaje_pago':
            msg = await client.send_message(uid,
                "¡Acción no permitida! 🚫\n\nDebes finalizar el viaje actual con el pasajero para poder terminar tu jornada."
            )
            log_message(msg)
            return # Detenemos la ejecución aquí, no se hace nada más.

        if current_state == 'pending_income':
            msg = await client.send_message(uid,
                "¡Acción no permitida! 🚫\n\nDebes finalizar el viaje actual con el pasajero para poder terminar tu jornada.\n👇 **Escribe el monto recibido por el viaje.**"
            )
            log_message(msg)
            return # Detenemos la ejecución aquí.
        
        # Si el código llega hasta aquí, significa que el estado es 'en_desplazamiento',
        # por lo que es seguro finalizar la jornada.
        await cb.answer("Finalizando jornada...")
        await stop_status_task(client, uid)

        # Generar y enviar el resumen ANTES de borrar los datos de la sesión
        summary_text = generate_final_summary(session)
        # Este es un mensaje importante, démosle 1 hora de vida antes de borrarse
        summary_msg = await client.send_message(uid, summary_text)
        log_message(summary_msg, hours=1)
        
        # Ahora sí, limpiar la sesión de la memoria y la base de datos
        sessions.pop(uid, None)
        delete_session_from_db(uid)

        # Intentar borrar el panel de estado anterior para una salida limpia
        try:
            await cb.message.delete()
        except Exception:
            pass

        # --- INICIO DE LA CORRECCIÓN ---
        # En lugar de llamar a start_cmd, replicamos aquí la lógica del menú principal.
        # 'uid' ya lo tenemos del callback (cb.from_user.id), así que es el correcto.
        user_data = get_user(uid)
        if user_data:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Iniciar Nueva Jornada", callback_data="info_start_trip")],
                [InlineKeyboardButton("✍️ Editar datos de mi vehículo", callback_data="edit_vehicle_data")]
            ])
            # Enviamos un nuevo mensaje con el menú principal
            menu_msg = await client.send_message(
                uid,
                f"¡Hola de nuevo, {user_data['name']}! 👋\n\nTu jornada ha finalizado. ¿Qué deseas hacer ahora?",
                reply_markup=keyboard
            )
            # Este menú es interactivo, pero no necesita ser permanente. Lo borramos en 1 hora.
            log_message(menu_msg, hours=1)
        # --- FIN DE LA CORRECCIÓN ---
        return

    elif data == "snooze_watchdog":
        session['watchdog_snoozed_until'] = datetime.now(timezone.utc) + timedelta(minutes=WATCHDOG_SNOOZE_MINUTES)
        save_session_to_db(uid)
        await cb.message.delete()

    elif data == "debug_generate_map":
        await cb.answer("Generando mapa...", show_alert=False)
        await generate_and_send_route_map(client, uid, session)
        return
    
async def stop_status_task(client, uid):
    session = sessions.get(uid)
    if session and session.get('status_task'):
        try: session['status_task'].cancel()
        except: pass
        session['status_task'] = None

# --- Bucle Principal ---
# --- INICIO: SECCIÓN FINAL DE ARRANQUE Y API ---

# Renombramos 'app' a 'bot_client' para claridad
bot_client = Client("data/uber_bot_final_session", api_id=API_ID, api_hash=API_HASH, bot_token=TOKEN)

@asynccontextmanager
async def lifespan(app_fastapi: FastAPI):
    """Gestiona el ciclo de vida completo del bot y servidor."""
    global bot_client
    logger.info("Ciclo de vida: Registrando handlers de Pyrogram...")
    
    # Registro manual de handlers para fiabilidad
    bot_client.add_handler(MessageHandler(start_cmd, filters.command("start") & filters.private))
    bot_client.add_handler(MessageHandler(setup_cmd, filters.command("setup") & filters.private))
    bot_client.add_handler(MessageHandler(text_handler, filters.text & filters.private))
    bot_client.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("Ciclo de vida: Iniciando Pyrogram...")
    async with bot_client:
        logger.info("Cliente de Pyrogram iniciado. Lanzando tareas de fondo...")
        asyncio.create_task(cleanup_task(bot_client))
        load_sessions_from_db()
        for user_id in list(sessions.keys()):
            logger.info(f"Reanudando sesión para el usuario {user_id}")
            await reset_status_message(bot_client, user_id)
        yield
    
    logger.info("Ciclo de vida: Pyrogram detenido. Cerrando conexión a la base de datos.")
    conn.close()

api = FastAPI(lifespan=lifespan)

@api.post("/api/traccar")
async def receive_traccar_data(payload: TraccarPayload = Body(...)):
    """Receptor principal de datos de Traccar."""
    user_id_str = payload.device_id
    lat = payload.location.coords.latitude
    lon = payload.location.coords.longitude
    speed_kmh = payload.location.coords.speed * 1.852
    
    logger.info(f"Recibido ping de Traccar para ID {user_id_str}: Lat={lat:.4f}, Lon={lon:.4f}, Vel={speed_kmh:.1f} km/h")

    try: uid = int(user_id_str)
    except ValueError: return {"status": "error", "message": "invalid id"}

    user_data = get_user(uid)
    if not user_data: return {"status": "error", "message": "unauthorized"}

    session = sessions.get(uid)
    if not session or session.get('current_state') in ['pending_income']: return {"status": "ok", "message": "session inactive"}

    now = payload.location.timestamp
    new_point = [lat, lon]

    target_segment = session.get('current_trip') if session['current_state'] == 'en_viaje_pago' else session.get('current_displacement')
    if not target_segment: return {"status": "error", "message": "no active segment"}

    path = target_segment.get('path', [])
    old_point = path[-1] if path else None
    path.append(new_point)
    session['last_update_ts'] = now

    if old_point:
        dist_haversine_km = haversine(old_point, new_point)
        if dist_haversine_km * 1000 < STOP_DETECTION_METERS:
            save_session_to_db(uid)
            return {"status": "ok", "message": "jitter ignored"}

        route_data, _ = await get_real_route_from_ors([old_point, new_point])
        dist_ors_km = route_data['features'][0]['properties']['summary']['distance'] / 1000.0 if route_data else 0

        dist_to_add_km = dist_haversine_km
        if dist_ors_km > 0:
            ratio = dist_ors_km / dist_haversine_km if dist_haversine_km > 0 else float('inf')
            if ratio <= DETOUR_RATIO_THRESHOLD: dist_to_add_km = dist_ors_km
        
        km_per_unit = user_data.get('km_per_unit', 40.0)
        price_per_unit = user_data.get('price_per_unit', 16182.0)
        if km_per_unit == 0: km_per_unit = 1

        target_segment['distance'] = target_segment.get('distance', 0) + dist_to_add_km
        target_segment['fuel_cost'] = target_segment.get('fuel_cost', 0) + ((dist_to_add_km / km_per_unit) * price_per_unit)
        await check_toll_crossing(bot_client, uid, old_point, new_point)
    
    save_session_to_db(uid)
    return {"status": "ok"}

if __name__ == "__main__":
    setup_database()
    logger.info("Iniciando Uvicorn (que gestionará FastAPI y Pyrogram)...")
    # Usa el puerto 9091 como en el bot de prueba.
    uvicorn.run(api, host="0.0.0.0", port=9091)