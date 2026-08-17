"""
Bot de Telegram - Optimizador de Rutas

holaaa
========================================
Mejoras respecto a la versión original:
  1. Credenciales vía variables de entorno (nunca hardcodeadas).
  2. Llamadas HTTP asíncronas con httpx (no bloquean el event loop).
  3. Persistencia de rutas en SQLite (sobrevive a reinicios del bot).
  4. No hace falta /start ni /nueva_ruta: cualquier ubicación o dirección
     que envíes se agrega como parada automáticamente.
  5. Comandos: /optimizar, /ver (listar paradas), /deshacer (quitar última
     parada), /nueva_ruta (borrar todo y empezar de cero), y opción de ruta
     "ida y vuelta" (cerrada) vs "solo ida" (abierta) al optimizar.
  6. Manejo de errores más granular (timeouts, geocodificación fallida, OSRM caído).
  7. Límite de paradas para evitar links de Google Maps rotos.
  8. NUEVO — Ruteo "vecino más cercano" con origen dinámico:
     - Si compartes un pin ESTÁTICO, se agrega como parada preestablecida
       (comportamiento igual al de siempre, vía Geoapify/OSRM en /optimizar).
     - Si compartes tu UBICACIÓN EN VIVO (live location), esa posición se
       usa como punto de partida P0 y se traza al instante una ruta por
       vecino más cercano (Haversine) contra las paradas ya guardadas, sin
       necesidad de /optimizar. Cada vez que Telegram reenvía tu posición
       actualizada (edited_message), la ruta se recalcula automáticamente
       desde tu nueva posición.

Requisitos:
    pip install python-telegram-bot httpx ortools python-dotenv

Variables de entorno requeridas (crea un archivo .env y NO lo subas a git):
    TELEGRAM_TOKEN=xxxx
    GEOAPIFY_KEY=xxxx
"""

import os
import re
import math
import sqlite3
import logging
import json
from contextlib import closing

from dotenv import load_dotenv
load_dotenv()  # Lee el archivo .env y carga sus variables en el entorno

import httpx
from telegram import Update, BotCommand, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from ortools.constraint_solver import routing_enums_pb2, pywrapcp

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEOAPIFY_KEY = os.environ.get("GEOAPIFY_KEY")

if not TELEGRAM_TOKEN or not GEOAPIFY_KEY:
    raise RuntimeError(
        "Faltan variables de entorno. Define TELEGRAM_TOKEN y GEOAPIFY_KEY "
        "(por ejemplo con un archivo .env + python-dotenv, o en tu panel de hosting)."
    )

DB_PATH = os.environ.get("ROUTES_DB_PATH", "rutas.db")
MAX_PARADAS = 23  # límite práctico antes de que la URL de Google Maps falle
OSRM_URL = os.environ.get("OSRM_URL", "http://router.project-osrm.org")

TECLADO_PRINCIPAL = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🔄 Nueva Ruta"), KeyboardButton("⚡ Optimizar Ruta")],
        [KeyboardButton("👀 Ver Paradas"), KeyboardButton("↩️ Deshacer")],
        [KeyboardButton("❓ Ayuda")],
    ],
    resize_keyboard=True,
)

TECLADO_TIPO_RUTA = ReplyKeyboardMarkup(
    [[KeyboardButton("➡️ Solo ida"), KeyboardButton("🔁 Ida y vuelta")]],
    resize_keyboard=True,
)

# request_location=True hace que, al tocar este botón, Telegram capture y
# envíe la ubicación GPS actual del dispositivo directamente (el usuario no
# puede "escribir" una ubicación falsa por aquí). Es la pieza que hace
# obligatoria la ubicación real antes de optimizar.
TECLADO_SOLICITAR_UBICACION = ReplyKeyboardMarkup(
    [[KeyboardButton("📍 Enviar mi ubicación actual", request_location=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


# ---------------------------------------------------------------------------
# Capa de persistencia (SQLite)
# ---------------------------------------------------------------------------

def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paradas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                orden INTEGER NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                nombre TEXT NOT NULL
            )
            """
        )
        conn.commit()


def db_agregar_parada(user_id: int, punto: dict):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT COALESCE(MAX(orden), -1) + 1 FROM paradas WHERE user_id = ?",
            (user_id,),
        )
        siguiente_orden = cur.fetchone()[0]
        conn.execute(
            "INSERT INTO paradas (user_id, orden, lat, lon, nombre) VALUES (?, ?, ?, ?, ?)",
            (user_id, siguiente_orden, punto["lat"], punto["lon"], punto["nombre"]),
        )
        conn.commit()
        return siguiente_orden + 1  # total actual


def db_obtener_paradas(user_id: int) -> list:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT lat, lon, nombre FROM paradas WHERE user_id = ? ORDER BY orden",
            (user_id,),
        )
        return [{"lat": r[0], "lon": r[1], "nombre": r[2]} for r in cur.fetchall()]


def db_limpiar_paradas(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM paradas WHERE user_id = ?", (user_id,))
        conn.commit()


def db_deshacer_ultima(user_id: int) -> str | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT id, nombre FROM paradas WHERE user_id = ? ORDER BY orden DESC LIMIT 1",
            (user_id,),
        )
        fila = cur.fetchone()
        if not fila:
            return None
        conn.execute("DELETE FROM paradas WHERE id = ?", (fila[0],))
        conn.commit()
        return fila[1]


# ---------------------------------------------------------------------------
# Geocodificación y routing (async) — flujo original vía OSRM / ortools
# ---------------------------------------------------------------------------

async def obtener_coordenadas_geoapify(texto_o_url: str, api_key: str, client: httpx.AsyncClient) -> dict | None:
    """Extrae coordenadas de URLs de Google Maps o geocodifica texto con Geoapify."""
    url_procesada = texto_o_url

    if "http" in texto_o_url:
        try:
            res = await client.get(
                texto_o_url,
                follow_redirects=True,
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            url_procesada = str(res.url)
        except httpx.RequestError as e:
            logger.warning(f"No se pudo resolver la URL corta: {e}")

    match_coords = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url_procesada)
    if match_coords:
        lat, lon = float(match_coords.group(1)), float(match_coords.group(2))
        return {"lat": lat, "lon": lon, "nombre": f"Ubicación ({lat:.4f}, {lon:.4f})"}

    match_q = re.search(r"[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)", url_procesada)
    if match_q:
        lat, lon = float(match_q.group(1)), float(match_q.group(2))
        return {"lat": lat, "lon": lon, "nombre": f"Ubicación ({lat:.4f}, {lon:.4f})"}

    geocode_url = "https://api.geoapify.com/v1/geocode/search"
    try:
        response = await client.get(
            geocode_url,
            params={"text": texto_o_url, "apiKey": api_key},
            timeout=8,
        )
        data = response.json()
        if data.get("features"):
            primer_resultado = data["features"][0]
            lon, lat = primer_resultado["geometry"]["coordinates"]
            propiedades = primer_resultado["properties"]
            nombre_formateado = propiedades.get("formatted", texto_o_url)
            return {"lat": lat, "lon": lon, "nombre": nombre_formateado}
    except httpx.RequestError as e:
        logger.error(f"Error de red en Geocoding: {e}")
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.error(f"Respuesta inesperada de Geoapify: {e}")

    return None


async def obtener_matriz_duracion_osrm(puntos: list, client: httpx.AsyncClient) -> list:
    """Calcula la matriz de tiempos entre puntos usando OSRM."""
    coords_str = ";".join([f"{p['lon']},{p['lat']}" for p in puntos])
    url_osrm = f"{OSRM_URL}/table/v1/driving/{coords_str}"

    try:
        response = await client.get(url_osrm, params={"annotations": "duration"}, timeout=15)
        data = response.json()
    except httpx.RequestError as e:
        raise RuntimeError(
            "No se pudo contactar el servicio de rutas (OSRM). Intenta de nuevo en unos minutos."
        ) from e
    except json.JSONDecodeError as e:
        raise RuntimeError("El servicio de rutas devolvió una respuesta inválida.") from e

    if response.status_code != 200 or "durations" not in data:
        mensaje = data.get("message", "Error al consultar OSRM Matrix API")
        raise RuntimeError(f"OSRM Error: {mensaje}")

    return [[int(t) if t is not None else 999999 for t in fila] for fila in data["durations"]]


def calcular_secuencia_fija_desde_origen(matriz_duracion: list, ida_y_vuelta: bool) -> list:
    """
    Resuelve el TSP (vía OR-Tools) igual que antes, pero con una diferencia
    fundamental: el nodo 0 de `matriz_duracion` YA NO es un depósito
    ficticio de costo cero que el solver puede tratar como si estuviera en
    cualquier parte. Aquí el nodo 0 es SIEMPRE la ubicación real y actual
    del usuario -obtenida obligatoriamente antes de optimizar-, así que el
    punto de partida queda anclado y el solver solo decide el orden de las
    paradas restantes (y, si aplica, el mejor punto donde terminar).

    - ida_y_vuelta=True  -> tour cerrado: nodo 0 sirve de depósito real de
      inicio Y fin (OR-Tools ya optimiza el regreso al origen dentro del
      costo total).
    - ida_y_vuelta=False -> ruta abierta con inicio fijo: se agrega un nodo
      virtual adicional con costo 0 hacia/desde todos los demás, usado
      exclusivamente como "fin libre" para que el solver pueda terminar en
      la parada real que más convenga sin forzar un regreso al origen.
    """
    num_puntos_totales = len(matriz_duracion)  # incluye el origen real en el índice 0

    if ida_y_vuelta:
        manager = pywrapcp.RoutingIndexManager(num_puntos_totales, 1, 0)
        matriz_para_callback = matriz_duracion
    else:
        nodo_final_virtual = num_puntos_totales
        matriz_para_callback = [fila + [0] for fila in matriz_duracion]
        matriz_para_callback.append([0] * (num_puntos_totales + 1))
        manager = pywrapcp.RoutingIndexManager(
            num_puntos_totales + 1, 1, [0], [nodo_final_virtual]
        )

    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return matriz_para_callback[from_node][to_node]

    transit_callback_index = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()

    # PARALLEL_CHEAPEST_INSERTION arma una primera ruta razonable evitando
    # zigzagueos obvios (mejor punto de partida que la heurística greedy simple).
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    )

    # Guided Local Search refina activamente esa primera ruta: reordena,
    # deshace cruces y retrocesos, y sigue buscando mejoras hasta el límite
    # de tiempo. Esto es lo que realmente minimiza la duración total del
    # recorrido en vez de quedarse con la primera solución greedy.
    search_parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    # Más paradas = más tiempo de búsqueda para converger a un buen óptimo,
    # con un tope razonable para no hacer esperar demasiado al usuario.
    tiempo_busqueda = max(3, min(20, num_puntos_totales))
    search_parameters.time_limit.FromSeconds(tiempo_busqueda)

    solution = routing.SolveWithParameters(search_parameters)

    orden_optimo = []
    if solution:
        index = routing.Start(0)
        while not routing.IsEnd(index):
            nodo = manager.IndexToNode(index)
            # nodo 0 = origen real del usuario -> no se lista como "parada".
            # El nodo virtual final (solo existe en modo ruta abierta) tampoco cuenta.
            if nodo != 0 and nodo < num_puntos_totales:
                orden_optimo.append(nodo - 1)  # índice dentro de la lista `paradas`
            index = solution.Value(routing.NextVar(index))

    return orden_optimo


def construir_url_waze(punto: dict) -> str:
    """
    Waze, a diferencia de Google Maps, no tiene un esquema de URL público
    para encadenar varias paradas en una sola ruta -su deep link solo acepta
    UN destino a la vez-. Por eso generamos un link de Waze por cada parada
    individual: el usuario toca la que le toca navegar en ese momento y Waze
    la abre directo, en vez de intentar (sin éxito) mandar la ruta completa.
    """
    return f"https://waze.com/ul?ll={punto['lat']}%2C{punto['lon']}&navigate=yes"


def construir_url_maps_ruta_optimizada(origen: dict, puntos_ordenados: list, ida_y_vuelta: bool) -> str:
    """
    Arma el link de Google Maps Directions para la ruta calculada por
    `calcular_secuencia_fija_desde_origen`, siempre partiendo de la
    ubicación real del usuario (`origen`).
    """
    if ida_y_vuelta:
        # El destino final es el propio origen (tour cerrado); todas las
        # paradas van como waypoints intermedios en el orden óptimo.
        destino = origen
        intermedios = puntos_ordenados
    else:
        destino = puntos_ordenados[-1]
        intermedios = puntos_ordenados[:-1]

    origin_str = f"{origen['lat']},{origen['lon']}"
    destination_str = f"{destino['lat']},{destino['lon']}"

    url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={origin_str}"
        f"&destination={destination_str}"
    )
    if intermedios:
        waypoints_str = "|".join(f"{p['lat']},{p['lon']}" for p in intermedios)
        url += f"&waypoints={waypoints_str}"
    url += "&travelmode=driving&dir_action=navigate"
    return url


# ---------------------------------------------------------------------------
# NUEVO: Ruteo por vecino más cercano con origen dinámico (ubicación en vivo)
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Distancia de Haversine entre dos coordenadas GPS, en kilómetros.

    Calcula la distancia en línea recta ("gran círculo") sobre la superficie
    terrestre asumida como esfera. No conoce calles ni tráfico real -es una
    aproximación geométrica-, pero es instantánea y no depende de ningún
    servicio externo, lo cual la hace ideal para recalcular la ruta en cada
    actualización de ubicación en vivo sin saturar la API de OSRM.

    Fórmula:
        a = sin²(Δφ/2) + cos(φ1)·cos(φ2)·sin²(Δλ/2)
        c = 2·atan2(√a, √(1−a))
        d = R·c
    donde φ = latitud, λ = longitud (en radianes) y R = radio de la Tierra.
    """
    R_TIERRA_KM = 6371.0

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R_TIERRA_KM * c


def calcular_ruta_vecino_mas_cercano(origen: dict, puntos: list) -> list:
    """
    Heurística "vecino más cercano" (Nearest Neighbor / greedy).

    Partiendo del `origen` (P0, típicamente la ubicación en vivo del
    usuario), en cada paso elige -entre los puntos aún no visitados- el más
    cercano a la posición actual, lo agrega a la ruta y avanza hacia él.
    Repite hasta agotar todos los puntos.

    No garantiza el óptimo global (eso lo hace `calcular_secuencia_fija_desde_origen`
    con OR-Tools + Guided Local Search para el flujo /optimizar), pero es
    O(n²) y extremadamente rápida, por lo que es apropiada para recalcularse
    en tiempo real cada vez que el usuario se mueve.

    Devuelve la lista de puntos en el orden a visitar; cada punto incluye
    además la clave 'distancia_km' = distancia Haversine desde la parada
    anterior en la ruta (o desde el origen, para el primer tramo).
    """
    no_visitados = list(puntos)
    posicion_actual = origen
    ruta_ordenada = []

    while no_visitados:
        # Calcula la distancia desde la posición actual a cada candidato
        # restante y se queda con el mínimo (búsqueda lineal: O(n) por paso).
        candidatos_con_distancia = [
            (haversine_km(posicion_actual["lat"], posicion_actual["lon"], p["lat"], p["lon"]), p)
            for p in no_visitados
        ]
        distancia_minima, mas_cercano = min(candidatos_con_distancia, key=lambda t: t[0])

        punto_visitado = dict(mas_cercano)
        punto_visitado["distancia_km"] = distancia_minima
        ruta_ordenada.append(punto_visitado)

        no_visitados.remove(mas_cercano)
        posicion_actual = mas_cercano  # el "vecino más cercano" pasa a ser el nuevo origen

    return ruta_ordenada


def construir_url_maps_desde_origen(origen: dict, ruta_ordenada: list) -> str:
    """
    Arma el link de Google Maps Directions respetando el orden exacto que
    produjo el vecino más cercano: origin = ubicación en vivo del usuario,
    waypoints = paradas intermedias en orden, destination = última parada.
    """
    destino = ruta_ordenada[-1]
    intermedios = ruta_ordenada[:-1]

    origin_str = f"{origen['lat']},{origen['lon']}"
    destination_str = f"{destino['lat']},{destino['lon']}"

    url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={origin_str}"
        f"&destination={destination_str}"
    )
    if intermedios:
        waypoints_str = "|".join(f"{p['lat']},{p['lon']}" for p in intermedios)
        url += f"&waypoints={waypoints_str}"
    url += "&travelmode=driving&dir_action=navigate"
    return url


async def procesar_origen_en_vivo(update: Update, context: ContextTypes.DEFAULT_TYPE, location) -> None:
    """
    Punto de entrada común para el primer envío de ubicación en vivo y para
    cada actualización posterior (edited_message). Recalcula la ruta por
    vecino más cercano tomando `location` como P0 y las paradas guardadas
    en SQLite como la lista de puntos preestablecidos.
    """
    user_id = update.effective_user.id
    lat, lon = location.latitude, location.longitude
    origen = {"lat": lat, "lon": lon, "nombre": "📍 Tu ubicación actual"}

    paradas = db_obtener_paradas(user_id)
    chat_id = update.effective_chat.id

    if not paradas:
        # Solo avisamos en el envío inicial (update.message existe); las
        # actualizaciones de vivo llegan como edited_message y no deberían
        # repetir el aviso en cada tick de GPS.
        if update.message is not None:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ Aún no tienes paradas preestablecidas. Agrega direcciones o pines "
                    "primero y luego comparte tu ubicación en vivo para trazar la ruta "
                    "desde donde estás."
                ),
                reply_markup=TECLADO_PRINCIPAL,
            )
        return

    ruta_ordenada = calcular_ruta_vecino_mas_cercano(origen, paradas)

    resumen = "🧭 <b>Ruta recalculada desde tu ubicación actual</b> (vecino más cercano):\n\n"
    resumen += f"🏁 Inicio: {origen['nombre']} ({lat:.4f}, {lon:.4f})\n\n"
    for idx, p in enumerate(ruta_ordenada, start=1):
        waze_url = construir_url_waze(p)
        resumen += f"{idx}. 📍 <a href='{waze_url}'>{p['nombre']}</a> — {p['distancia_km']:.2f} km desde el punto anterior\n"

    distancia_total_km = sum(p["distancia_km"] for p in ruta_ordenada)
    resumen += f"\n📏 Distancia total aproximada (línea recta): {distancia_total_km:.2f} km\n"

    maps_url = construir_url_maps_desde_origen(origen, ruta_ordenada)
    resumen += f"\n🗺️ <b><a href='{maps_url}'>👉 TOCAR AQUÍ PARA INICIAR LA RUTA EN MAPS</a></b>\n\n"
    resumen += "<i>Cada parada también abre directo en Waze (un destino a la vez). Comparte tu ubicación en vivo de nuevo (o deja que se actualice) para recalcular desde tu nueva posición.</i>"

    await context.bot.send_message(chat_id=chat_id, text=resumen, parse_mode="HTML", disable_web_page_preview=False)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def post_init(application):
    comandos = [
        BotCommand("nueva_ruta", "Iniciar un nuevo itinerario de paradas"),
        BotCommand("optimizar", "Calcular y ordenar la ruta más rápida"),
        BotCommand("ver", "Ver las paradas guardadas hasta ahora"),
        BotCommand("deshacer", "Quitar la última parada agregada"),
        BotCommand("ayuda", "Instrucciones de uso del bot"),
    ]
    await application.bot.set_my_commands(comandos)


async def cmd_start_o_nueva(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_limpiar_paradas(user_id)
    context.user_data["esperando_tipo_ruta"] = False
    context.user_data["esperando_ubicacion_optimizar"] = False
    context.user_data["origen_optimizar"] = None
    await update.message.reply_text(
        "🔄 <b>Nueva ruta iniciada.</b>\n\n"
        "Envíame las ubicaciones (pines GPS, direcciones o enlaces) <b>una a una</b>.\n"
        "Cuando termines, presiona <b>⚡ Optimizar Ruta</b> o usa /optimizar; te voy a pedir tu "
        "<b>ubicación actual</b> para calcular la ruta más eficiente desde donde estás.\n\n"
        "💡 Tip: si compartes tu <b>ubicación en vivo</b> fuera de /optimizar, trazo al instante "
        "la ruta más cercana desde donde estás y la recalculo mientras te mueves.\n\n"
        "Otros comandos útiles: 👀 Ver Paradas · ↩️ Deshacer",
        parse_mode="HTML",
        reply_markup=TECLADO_PRINCIPAL,
    )


async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Instrucciones:</b>\n\n"
        "1. Simplemente envíame tus destinos uno a uno (GPS, dirección escrita o link) — no necesitas ningún comando para empezar.\n"
        "2. Usa 👀 Ver Paradas para revisar lo agregado, o ↩️ Deshacer si te equivocas.\n"
        f"3. Máximo {MAX_PARADAS} paradas por ruta.\n"
        "4. Usa /optimizar: te voy a pedir tu <b>ubicación actual</b> con un botón — es "
        "obligatoria, no puedo calcular la ruta más eficiente sin saber desde dónde partes.\n"
        "5. Luego te preguntaré si quieres regresar a tu punto de partida al final, o solo "
        "llegar al último destino.\n"
        "6. Usa 🔄 Nueva Ruta en cualquier momento para borrar todo y empezar de cero.\n"
        "7. 📍 Fuera de /optimizar: si compartes un pin fijo, se guarda como parada. Si "
        "compartes tu <b>ubicación en vivo</b>, la uso como punto de partida y trazo al "
        "instante la ruta más cercana (vecino más cercano), recalculándola mientras te mueves.",
        parse_mode="HTML",
        reply_markup=TECLADO_PRINCIPAL,
    )


async def cmd_ver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    paradas = db_obtener_paradas(user_id)
    if not paradas:
        await update.message.reply_text("📭 No tienes paradas guardadas todavía. Envíame una ubicación o dirección para empezar.")
        return
    texto = "📋 <b>Paradas guardadas:</b>\n\n" + "\n".join(
        f"{i}. {p['nombre']}" for i, p in enumerate(paradas, start=1)
    )
    await update.message.reply_text(texto, parse_mode="HTML")


async def cmd_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    eliminada = db_deshacer_ultima(user_id)
    if eliminada is None:
        await update.message.reply_text("⚠️ No hay paradas para deshacer.")
    else:
        await update.message.reply_text(f"↩️ Se eliminó: <i>{eliminada}</i>", parse_mode="HTML")


async def recibir_ubicacion_nativa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Punto de entrada para el PRIMER mensaje de ubicación (message.location).

    - Si el usuario está a mitad del flujo de /optimizar (esperando_tipo_ruta),
      se le pide que responda esa pregunta primero.
    - Si `location.live_period` está presente, es una ubicación EN VIVO: se
      usa como origen dinámico P0 y se dispara el ruteo por vecino más
      cercano (requerimientos 1, 2 y 3). Las actualizaciones posteriores de
      esta misma ubicación en vivo llegan como `edited_message` y las
      captura `recibir_ubicacion_editada` (requerimiento 4).
    - Si es un pin ESTÁTICO (sin live_period), se conserva el comportamiento
      original: se agrega como parada preestablecida para /optimizar.
    """
    user_id = update.effective_user.id
    location = update.message.location

    # Prioridad máxima: si estamos a mitad del flujo de /optimizar esperando
    # la ubicación OBLIGATORIA del usuario, cualquier location que llegue se
    # toma como el punto de partida fijo para el cálculo -no se agrega como
    # parada suelta-.
    if context.user_data.get("esperando_ubicacion_optimizar"):
        context.user_data["esperando_ubicacion_optimizar"] = False
        context.user_data["origen_optimizar"] = {
            "lat": location.latitude,
            "lon": location.longitude,
            "nombre": "📍 Tu ubicación actual",
        }
        context.user_data["esperando_tipo_ruta"] = True
        await update.message.reply_text(
            "✅ Ubicación recibida. ¿Quieres regresar a tu punto de partida al final de la "
            "ruta, o solo llegar al último destino?",
            reply_markup=TECLADO_TIPO_RUTA,
        )
        return

    if context.user_data.get("esperando_tipo_ruta"):
        await update.message.reply_text("Primero respóndeme ➡️ Solo ida o 🔁 Ida y vuelta. 👇", reply_markup=TECLADO_TIPO_RUTA)
        return

    if location.live_period:
        await procesar_origen_en_vivo(update, context, location)
        return

    lat, lon = location.latitude, location.longitude

    if len(db_obtener_paradas(user_id)) >= MAX_PARADAS:
        await update.message.reply_text(f"⚠️ Alcanzaste el máximo de {MAX_PARADAS} paradas. Usa /optimizar o 🔄 Nueva Ruta.")
        return

    punto_info = {"lat": lat, "lon": lon, "nombre": f"Parada GPS ({lat:.4f}, {lon:.4f})"}
    total = db_agregar_parada(user_id, punto_info)
    await update.message.reply_text(
        f"📍 Parada #{total} guardada: <i>{punto_info['nombre']}</i>",
        parse_mode="HTML",
        reply_markup=TECLADO_PRINCIPAL,
    )


async def recibir_ubicacion_editada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Telegram no manda un `message` nuevo por cada tick de una ubicación en
    vivo: reenvía el mismo mensaje editado (`edited_message`) con las
    coordenadas actualizadas mientras el usuario se mueve. Este handler
    captura esas ediciones y vuelve a correr el vecino más cercano desde la
    nueva posición (requerimiento 4: recalcular la ruta si el usuario
    vuelve a compartir/actualizar su ubicación).
    """
    location = update.edited_message.location if update.edited_message else None
    if location is None or not location.live_period:
        return
    await procesar_origen_en_vivo(update, context, location)


async def manejar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Único handler de texto libre: decide si es un botón, la respuesta a
    'tipo de ruta', o una nueva parada a geocodificar. No requiere /start previo."""
    user_id = update.effective_user.id
    texto = update.message.text.strip()

    # Botones del teclado principal (siempre disponibles, incluso para
    # cancelar/reiniciar mientras se espera la ubicación obligatoria)
    if texto == "🔄 Nueva Ruta":
        return await cmd_start_o_nueva(update, context)
    elif texto == "👀 Ver Paradas":
        return await cmd_ver(update, context)
    elif texto == "↩️ Deshacer":
        return await cmd_deshacer(update, context)
    elif texto == "❓ Ayuda":
        return await cmd_ayuda(update, context)

    # Si estamos esperando la ubicación obligatoria para /optimizar, ningún
    # texto libre la reemplaza -incluido "⚡ Optimizar Ruta" de nuevo-: se
    # insiste en el botón de ubicación hasta recibir coordenadas reales.
    if context.user_data.get("esperando_ubicacion_optimizar"):
        await update.message.reply_text(
            "📍 Necesito tu <b>ubicación actual</b> para calcular la ruta más eficiente. "
            "Por favor usa el botón de abajo (no puedo aceptar una ubicación escrita aquí).",
            parse_mode="HTML",
            reply_markup=TECLADO_SOLICITAR_UBICACION,
        )
        return

    if texto == "⚡ Optimizar Ruta":
        return await cmd_optimizar(update, context)

    # Si el bot está esperando la respuesta de ida/vuelta, la respuesta manda aquí
    if context.user_data.get("esperando_tipo_ruta"):
        return await recibir_tipo_ruta(update, context)

    # Si nada de lo anterior aplica, es una nueva parada por geocodificar
    if len(db_obtener_paradas(user_id)) >= MAX_PARADAS:
        await update.message.reply_text(f"⚠️ Alcanzaste el máximo de {MAX_PARADAS} paradas. Usa /optimizar o 🔄 Nueva Ruta.")
        return

    async with httpx.AsyncClient() as client:
        punto_info = await obtener_coordenadas_geoapify(texto, GEOAPIFY_KEY, client)

    if not punto_info:
        await update.message.reply_text("❌ No se pudo determinar la ubicación. Intenta enviar el pin GPS directamente o revisa la dirección.")
        return

    total = db_agregar_parada(user_id, punto_info)
    await update.message.reply_text(
        f"📍 Parada #{total} guardada: <i>{punto_info['nombre']}</i>",
        parse_mode="HTML",
        reply_markup=TECLADO_PRINCIPAL,
    )


async def cmd_optimizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Arranca el flujo de optimización. Ya NO pregunta directo ida/vuelta:
    primero EXIGE la ubicación actual del usuario (vía botón de Telegram con
    request_location=True, así que no se puede falsear con texto), porque
    esa ubicación será el punto de partida fijo del cálculo.
    """
    user_id = update.effective_user.id
    paradas = db_obtener_paradas(user_id)

    if len(paradas) < 1:
        await update.message.reply_text("⚠️ Debes agregar al menos 1 parada antes de optimizar.")
        return

    context.user_data["esperando_tipo_ruta"] = False
    context.user_data["esperando_ubicacion_optimizar"] = True
    await update.message.reply_text(
        "📍 Para calcular la ruta más eficiente necesito tu <b>ubicación actual</b> como punto "
        "de partida.\n\nToca el botón de abajo para compartirla — es obligatorio, no puedo "
        "optimizar sin saber desde dónde partes.",
        parse_mode="HTML",
        reply_markup=TECLADO_SOLICITAR_UBICACION,
    )


async def recibir_tipo_ruta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if texto not in ("➡️ Solo ida", "🔁 Ida y vuelta"):
        await update.message.reply_text("Por favor selecciona una opción del teclado. 👇", reply_markup=TECLADO_TIPO_RUTA)
        return

    context.user_data["esperando_tipo_ruta"] = False
    ida_y_vuelta = texto == "🔁 Ida y vuelta"
    user_id = update.effective_user.id
    paradas = db_obtener_paradas(user_id)
    origen = context.user_data.get("origen_optimizar")

    # Salvaguarda: si por alguna razón se perdió el estado (ej. reinicio del
    # proceso entre la ubicación y esta respuesta), no seguimos sin origen.
    if origen is None:
        await update.message.reply_text(
            "⚠️ Perdí tu ubicación. Por favor usa /optimizar de nuevo para compartirla otra vez.",
            reply_markup=TECLADO_PRINCIPAL,
        )
        return

    if len(paradas) < 1:
        await update.message.reply_text("⚠️ Ya no tienes paradas guardadas.", reply_markup=TECLADO_PRINCIPAL)
        return

    await update.message.reply_text("⏳ Calculando la ruta más eficiente desde tu ubicación...", reply_markup=TECLADO_PRINCIPAL)

    try:
        # El origen real del usuario siempre va en el índice 0 de la matriz
        # que se manda a OSRM, para que el solver lo trate como punto fijo.
        puntos_totales = [origen] + paradas
        async with httpx.AsyncClient() as client:
            matriz = await obtener_matriz_duracion_osrm(puntos_totales, client)

        indices_ordenados = calcular_secuencia_fija_desde_origen(matriz, ida_y_vuelta)

        if not indices_ordenados:
            await update.message.reply_text("❌ No se pudo determinar el orden óptimo. Intenta con otras paradas.")
            return

        puntos_ordenados = [paradas[i] for i in indices_ordenados]

        maps_url = construir_url_maps_ruta_optimizada(origen, puntos_ordenados, ida_y_vuelta)

        tipo_label = "Ida y vuelta 🔁" if ida_y_vuelta else "Solo ida ➡️"
        resumen = f"✅ <b>Ruta Óptima desde tu ubicación</b> ({tipo_label}):\n\n"
        resumen += f"🏁 Punto de partida: {origen['nombre']}\n"
        for idx, p in enumerate(puntos_ordenados, start=1):
            waze_url = construir_url_waze(p)
            resumen += f"{idx}. 📍 <a href='{waze_url}'>{p['nombre']}</a>\n"
        if ida_y_vuelta:
            resumen += f"{len(puntos_ordenados) + 1}. 🏁 Regreso a: {origen['nombre']}\n"

        resumen += f"\n🗺️ <b><a href='{maps_url}'>👉 TOCAR AQUÍ PARA INICIAR LA RUTA COMPLETA EN MAPS</a></b>"
        resumen += "\n<i>Cada parada de la lista de arriba también abre directo en Waze (un destino a la vez, tócala cuando vayas hacia allá).</i>"

        await update.message.reply_text(resumen, parse_mode="HTML", disable_web_page_preview=False)

        db_limpiar_paradas(user_id)
        context.user_data["origen_optimizar"] = None

    except RuntimeError as e:
        logger.error(f"Error de servicio al optimizar: {e}")
        await update.message.reply_text(f"❌ {str(e)}")
    except Exception:
        logger.exception("Error inesperado al optimizar la ruta")
        await update.message.reply_text("❌ Ocurrió un error inesperado. Ya quedó registrado en los logs; intenta de nuevo.")


async def comando_desconocido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Se dispara con cualquier comando que no exista (ej. /xyz)."""
    await update.message.reply_text(
        "🤖 <b>¿Qué puedo hacer por ti?</b>\n\n"
        "🔄 <b>/nueva_ruta</b> — Borrar todo y empezar de cero\n"
        "⚡ <b>/optimizar</b> — Calcular el orden más rápido de tus paradas\n"
        "👀 <b>/ver</b> — Ver las paradas que ya guardaste\n"
        "↩️ <b>/deshacer</b> — Quitar la última parada agregada\n"
        "❓ <b>/ayuda</b> — Instrucciones detalladas de uso\n\n"
        "No necesitas ningún comando para agregar paradas: solo envíame una ubicación o dirección. 👇",
        parse_mode="HTML",
        reply_markup=TECLADO_PRINCIPAL,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start_o_nueva))
    app.add_handler(CommandHandler("nueva_ruta", cmd_start_o_nueva))
    app.add_handler(CommandHandler("optimizar", cmd_optimizar))
    app.add_handler(CommandHandler("ver", cmd_ver))
    app.add_handler(CommandHandler("deshacer", cmd_deshacer))
    app.add_handler(CommandHandler("ayuda", cmd_ayuda))

    # Ubicación GPS (primer envío) y texto libre se procesan siempre, sin
    # necesidad de /start previo.
    app.add_handler(MessageHandler(filters.LOCATION, recibir_ubicacion_nativa))
    # Actualizaciones de ubicación EN VIVO llegan como mensajes editados:
    # este handler recalcula la ruta en cada tick mientras el usuario se mueve.
    app.add_handler(
        MessageHandler(filters.LOCATION & filters.UpdateType.EDITED_MESSAGE, recibir_ubicacion_editada)
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_texto))

    # Cualquier comando no reconocido (ej. /xyz) muestra el menú de ayuda
    app.add_handler(MessageHandler(filters.COMMAND, comando_desconocido))

    logger.info(
        "🤖 Bot listo con persistencia SQLite, HTTP async, flujo simplificado "
        "y ruteo por vecino más cercano desde ubicación en vivo..."
    )
    app.run_polling()


if __name__ == "__main__":
    main()