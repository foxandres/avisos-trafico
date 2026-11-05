#!/usr/bin/env python3
"""
Bot de avisos de tráfico -> Telegram  (v4)

Fuentes oficiales:
  - DGT (Punto de Acceso Nacional): incidencias en carreteras.
  - Ayuntamiento de Madrid: incidencias en calles y sensores de tráfico en tiempo real.
  - Avisos meteorológicos oficiales de AEMET, leídos de MeteoAlarm (sin clave).

Qué te AVISA (con notificación):
  - Cortes de carretera o calzada, accidentes, meteorología adversa, retenciones con tráfico
    detenido o colas, salidas/entradas cerradas.
  - Avisos de AEMET en las zonas de tus carreteras ("zonas_aviso" en CONFIG): nevadas (desde amarillo) y lluvias o tormentas
    (solo rojo, configurable).
  - Si algo que ya te avisó empeora a corte total.
  - Cuando se resuelve un corte o accidente del que te avisó (sin sonido).

Qué NO avisa (aparece en el panel fijado, una línea corta cada una):
  - Obras sin corte, vehículos detenidos, obstáculos, tráfico lento o denso.
  - Estado del tráfico de tus calles de Madrid (fluido / lento / retenciones / congestión).

Para no repetir:
  - Una incidencia de la DGT con varias partes (corte + desvío + retención) es UN solo aviso.
  - Algo "resuelto" tiene que llevar 15 min sin aparecer.
  - Si aparece otra incidencia del mismo tipo en el mismo punto (±2 km, mismo sentido)
    en la última hora, no se vuelve a avisar.
  - En horario de silencio solo avisan los cortes totales y los avisos rojos.

Botones del panel: pausar 1 día / 1 semana y reanudar. El bot los atiende en la
siguiente revisión (como mucho unos 5 minutos).

Tus tramos y calles NO están en este código: se leen del secret CONFIG (JSON).
Variables de entorno: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID y CONFIG.
Solo usa la librería estándar de Python.
"""

import io
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ZONA = ZoneInfo("Europe/Madrid")
except Exception:  # pragma: no cover
    ZONA = timezone.utc

URL_DGT = "https://nap.dgt.es/datex2/v3/dgt/SituationPublication/datex2_v37.xml"
URL_MADRID = "https://datos.madrid.es/egob/catalogo/202062-0-trafico-incidencias-viapublica.xml"
URL_SENSORES_MADRID = "https://informo.madrid.es/informo/tmadrid/pm.xml"
URL_METEOALARM = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-spain"  # avisos de AEMET, sin clave
FICHERO_ESTADO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "estado.json")
XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
NUM = re.compile(r"^-?\d+(\.\d+)?$")
VERSION_ESTADO = 3

AUSENCIAS_PARA_RESUELTO = 3        # revisiones seguidas sin aparecer (~15 min)
VENTANA_REPETIDOS = timedelta(minutes=60)
DISTANCIA_REPETIDOS_KM = 2.0
CLASES_CON_RESUELTO = {"corte", "accidente"}
CADA_METEO = timedelta(minutes=15)  # los avisos cambian pocas veces al día
MAX_MENORES_POR_TRAMO = 6
LIMITE_PANEL = 3900                 # Telegram admite 4096 caracteres por mensaje
MAX_EN_RESUMEN = 6


# ============================================================
# Configuración (desde el secret CONFIG)
# ============================================================

def cargar_config():
    texto = os.environ.get("CONFIG", "").strip()
    if not texto:
        ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        if os.path.exists(ruta):
            with open(ruta, encoding="utf-8") as f:
                texto = f.read()
    if not texto:
        sys.exit("Falta la configuración: crea el secret CONFIG (mira config.ejemplo.json).")
    try:
        cfg = json.loads(texto)
    except json.JSONDecodeError as e:
        sys.exit(f"El secret CONFIG no es un JSON válido (línea {e.lineno}, columna {e.colno}).")
    cfg.setdefault("tramos", [])
    cfg.setdefault("calles", {})
    cfg.setdefault("silencio_utc", None)          # p. ej. [22, 5]
    cfg.setdefault("hacia_madrid_negativo", [])   # carreteras cuyos km bajan hacia Madrid
    meteo = cfg.setdefault("avisos_meteo", cfg.get("aemet", {}))
    meteo.setdefault("nieve_desde", "amarillo")
    meteo.setdefault("lluvia_tormenta_desde", "rojo")
    for t in cfg["tramos"]:
        t["carretera"] = t["carretera"].upper().replace(" ", "")
    return cfg


# ============================================================
# Utilidades
# ============================================================

def local(tag):
    return tag.rsplit("}", 1)[-1]


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", texto).strip()


def parse_fecha(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=ZONA)
    except ValueError:
        return None


def hora_local(dt, ahora=None, con_dia=True):
    if not dt:
        return ""
    dt = dt.astimezone(ZONA)
    hoy = (ahora or datetime.now(timezone.utc)).astimezone(ZONA).date()
    if con_dia and dt.date() != hoy:
        return dt.strftime("%d/%m %H:%M")
    return dt.strftime("%H:%M")


def dia_y_hora(dt, ahora):
    """'hoy 06:00', 'mañana 06:00' o '08/11 06:00'."""
    dt = dt.astimezone(ZONA)
    hoy = ahora.astimezone(ZONA).date()
    if dt.date() == hoy:
        dia = "hoy"
    elif dt.date() == hoy + timedelta(days=1):
        dia = "mañana"
    else:
        dia = dt.strftime("%d/%m")
    return f"{dia} {dt.strftime('%H:%M')}"


def fkm(k):
    return f"{round(k, 1):g}".replace(".", ",")


def esc(texto):
    return (texto or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def enlace_mapa(lat, lon):
    return f'<a href="https://maps.google.com/?q={lat:.5f},{lon:.5f}">Ver mapa</a>'


def descargar(url, cabeceras=None):
    req = urllib.request.Request(url, headers={"User-Agent": "avisos-trafico-bot/4.0", **(cabeceras or {})})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def en_silencio(cfg, ahora_utc=None):
    rango = cfg.get("silencio_utc")
    if not rango:
        return False
    h = (ahora_utc or datetime.now(timezone.utc)).hour
    ini, fin = rango
    return (ini <= h < fin) if ini < fin else (h >= ini or h < fin)



# ============================================================
# Clasificación de la DGT
# ============================================================

NOMBRES_CAUSA = {
    "accident": "Accidente",
    "abnormalTraffic": "Retención",
    "vehicleObstruction": "Vehículo detenido",
    "obstruction": "Obstáculo en la vía",
    "generalObstruction": "Obstáculo en la vía",
    "animalPresenceObstruction": "Animales en la vía",
    "environmentalObstruction": "Obstáculo en la vía",
    "infrastructureDamageObstruction": "Daños en la vía",
    "poorEnvironment": "Meteorología adversa",
    "poorRoadInfrastructure": "Problema en la vía",
    "publicEvent": "Evento",
    "fire": "Incendio",
    "roadMaintenance": "Obras",
}

TIPOS_REGISTRO = {
    "sit:Accident": "accident",
    "sit:AbnormalTraffic": "abnormalTraffic",
    "sit:VehicleObstruction": "vehicleObstruction",
    "sit:GeneralObstruction": "generalObstruction",
    "sit:AnimalPresenceObstruction": "animalPresenceObstruction",
    "sit:PoorEnvironmentConditions": "poorEnvironment",
    "sit:MaintenanceWorks": "roadMaintenance",
    "sit:Roadworks": "roadMaintenance",
    "sit:PublicEvent": "publicEvent",
}

OBSTACULOS = {"vehicleObstruction", "obstruction", "generalObstruction",
              "animalPresenceObstruction", "environmentalObstruction"}

METEO = {
    "snowfall": ("❄️", "Nieve"), "ice": ("🧊", "Hielo"), "blackIce": ("🧊", "Hielo"),
    "fog": ("🌫️", "Niebla"), "denseFog": ("🌫️", "Niebla densa"),
    "heavyRain": ("🌧️", "Lluvia intensa"), "strongWinds": ("💨", "Viento fuerte"),
    "flooding": ("🌊", "Inundación"), "snowOnTheRoad": ("❄️", "Nieve en la calzada"),
}

TRAFICO_SERIO = {"stationaryTraffic": "tráfico detenido", "queuingTraffic": "colas"}
TRAFICO_LEVE = {"slowTraffic": "tráfico lento", "heavyTraffic": "tráfico denso"}

CIERRES_TOTALES = {"roadClosed", "carriagewayClosures"}
CIERRES_PARCIALES = {"exitClosed", "entryClosed", "contraflow"}

EFECTOS = {
    "roadClosed": "carretera cortada",
    "carriagewayClosures": "calzada cortada",
    "exitClosed": "salida cerrada",
    "entryClosed": "entrada cerrada",
    "contraflow": "circulación por el carril contrario",
    "narrowLanes": "carriles estrechos",
    "singleAlternateLineTraffic": "paso alternativo",
    "lanesDeviated": "desvío de carriles",
    "newRoadworksLayout": "cambio de trazado",
    "hardShoulderClosed": "arcén cortado",
}

CARRILES = {
    "allLanesCompleteCarriageway": "todos los carriles",
    "leftLane": "carril izquierdo", "rightLane": "carril derecho",
    "middleLane": "carril central", "rightHardShoulder": "arcén derecho",
    "leftHardShoulder": "arcén izquierdo",
}

# clase: (rango de gravedad, icono, ¿avisa?)
CLASES = {
    "corte":     (0, "🚫", True),
    "aemet3":    (0, "🔴", True),   # aviso rojo de AEMET
    "accidente": (1, "💥", True),
    "aemet2":    (2, "🟠", True),   # aviso naranja
    "meteo":     (2, "🌧️", True),
    "aemet1":    (3, "🟡", True),   # aviso amarillo
    "retencion": (3, "🚗", True),
    "cierre":    (4, "⛔", True),
    "otro":      (4, "⚠️", True),
    "lento":     (5, "🚗", False),
    "vehiculo":  (6, "⚠️", False),
    "obra":      (7, "🚧", False),
}


def campos_registro(elem):
    campos = {}
    for e in elem.iter():
        if len(e):
            continue
        nombre = local(e.tag)
        texto = e.get("_extendedValue") if nombre == "laneUsage" and e.get("_extendedValue") \
            else (e.text or "").strip()
        if texto:
            campos.setdefault(nombre, []).append(texto)
    return campos


def analizar_registro(elem, campos, kms=()):
    """Clasifica un registro de la DGT. Devuelve dict con clase, icono y titular."""
    causa = TIPOS_REGISTRO.get(elem.get(XSI_TYPE, "")) or (campos.get("causeType") or [""])[0]
    gestion = (campos.get("roadOrCarriagewayOrLaneManagementType") or [""])[0]
    carriles = [CARRILES.get(c, c) for c in dict.fromkeys(campos.get("laneUsage", []))]
    tipos_trafico = campos.get("abnormalTrafficType", [])
    meteo = next((METEO[v] for v in campos.get("poorEnvironmentType", [])
                  + campos.get("weatherRelatedRoadConditionType", []) if v in METEO), None)

    if gestion == "laneClosures":
        if carriles == ["todos los carriles"]:
            gestion, efecto = "carriagewayClosures", "calzada cortada"
        else:
            efecto = f"{', '.join(carriles)} cortado" if carriles else "carril cortado"
    else:
        efecto = EFECTOS.get(gestion, "")

    serio = next((TRAFICO_SERIO[v] for v in tipos_trafico if v in TRAFICO_SERIO), "")
    leve = next((TRAFICO_LEVE[v] for v in tipos_trafico if v in TRAFICO_LEVE), "")
    nombre = NOMBRES_CAUSA.get(causa, "")
    icono_meteo = None
    if meteo:
        icono_meteo, nombre = meteo

    if gestion in CIERRES_TOTALES:
        clase = "corte"
    elif causa == "accident":
        clase = "accidente"
    elif meteo or causa == "poorEnvironment":
        clase = "meteo"
    elif causa == "abnormalTraffic" or tipos_trafico:
        clase = "retencion" if serio or not leve else "lento"
    elif causa in OBSTACULOS:
        clase = "vehiculo"
    elif causa in ("roadMaintenance", ""):
        clase = "cierre" if gestion in CIERRES_PARCIALES else "obra"
    else:
        clase = "otro"

    # Longitud de la retención, a partir del km de inicio y de fin
    if clase in ("retencion", "lento") and len(kms) >= 2:
        largo = max(kms) - min(kms)
        if largo >= 0.1:
            nombre = f"Retención de {fkm(largo)} km"

    rango, icono, avisa = CLASES[clase]
    if clase == "meteo" and icono_meteo:
        icono = icono_meteo
    partes = [p for p in (nombre, serio or leve, efecto) if p]
    titular = " · ".join(partes) or "Incidencia"
    resto = " · ".join(p for p in (serio or leve, efecto) if p)
    return {"clase": clase, "rango": rango, "icono": icono, "avisa": avisa, "nombre": nombre,
            "resto": resto, "titular": titular[:1].upper() + titular[1:]}


def tramo_que_coincide(tramos, carretera, kms, lons, lats):
    for tramo in tramos:
        if carretera != tramo["carretera"]:
            continue
        if "km_min" in tramo:
            if any(tramo["km_min"] <= k <= tramo["km_max"] for k in kms):
                return tramo
            if len(kms) >= 2 and min(kms) <= tramo["km_min"] and max(kms) >= tramo["km_max"]:
                return tramo
        if "lat_min" in tramo:
            if any(tramo["lat_min"] <= la <= tramo["lat_max"] and tramo["lon_min"] <= lo <= tramo["lon_max"]
                   for la, lo in zip(lats, lons)):
                return tramo
        elif "lon_min" in tramo:
            if any(tramo["lon_min"] <= lo <= tramo["lon_max"] for lo in lons):
                return tramo
    return None


def describir_sentido(direccion, carretera, cfg):
    if direccion == "both":
        return "ambos sentidos"
    if carretera in cfg["hacia_madrid_negativo"]:
        return {"positive": "saliendo de Madrid", "negative": "hacia Madrid"}.get(direccion, "")
    return {"positive": "km crecientes", "negative": "km decrecientes"}.get(direccion, "")


def registros_de_situacion(sit, cfg, ahora):
    """Registros válidos de una situación de la DGT que caen en tus tramos."""
    validos = []
    for rec in sit:
        if local(rec.tag) != "situationRecord":
            continue
        campos = campos_registro(rec)
        carretera = (campos.get("roadName") or [""])[0].upper().replace(" ", "")
        kms = [float(k) for k in campos.get("kilometerPoint", []) if NUM.match(k)]
        lons = [float(x) for x in campos.get("longitude", []) if NUM.match(x)]
        lats = [float(x) for x in campos.get("latitude", []) if NUM.match(x)]
        tramo = tramo_que_coincide(cfg["tramos"], carretera, kms, lons, lats)
        if not tramo:
            continue
        if (campos.get("validityStatus") or ["active"])[0] not in ("active", "planned"):
            continue
        inicio = parse_fecha((campos.get("overallStartTime") or [""])[0])
        fin = parse_fecha((campos.get("overallEndTime") or [""])[0])
        if (inicio and inicio > ahora) or (fin and fin < ahora):
            continue
        info = analizar_registro(rec, campos, kms)
        info.update({"tramo": tramo, "carretera": carretera, "kms": kms, "lats": lats, "lons": lons,
                     "municipios": campos.get("municipality", []), "inicio": inicio, "fin": fin,
                     "direccion": (campos.get("tpegDirectionRoad") or ["unknown"])[0]})
        validos.append(info)
    return validos


def incidencias_dgt(cfg, ahora):
    carreteras = {t["carretera"] for t in cfg["tramos"]}
    if not carreteras:
        return {}
    resultado = {}
    for _ev, elem in ET.iterparse(io.BytesIO(descargar(URL_DGT)), events=("end",)):
        if local(elem.tag) != "situation":
            continue
        try:
            # Descarte rápido: ninguna carretera de la situación nos interesa
            nombres = {(e.text or "").strip().upper().replace(" ", "")
                       for e in elem.iter() if local(e.tag) == "roadName"}
            if not nombres & carreteras:
                continue
            registros = registros_de_situacion(elem, cfg, ahora)
            if not registros:
                continue
            registros.sort(key=lambda r: r["rango"])
            p = registros[0]  # el más grave manda
            kms = [k for r in registros if r["carretera"] == p["carretera"] for k in r["kms"]]
            km_txt = ""
            if kms:
                km_txt = f"km {fkm(min(kms))}" if round(min(kms), 1) == round(max(kms), 1) \
                    else f"km {fkm(min(kms))}–{fkm(max(kms))}"
            municipios = list(dict.fromkeys(m for r in registros for m in r["municipios"]))
            lugar = " · ".join(x for x in (f"{p['carretera']} {km_txt}".strip(), ", ".join(municipios[:2])) if x)

            titular = p["titular"]
            extra = [(r["resto"] if r["nombre"] == p["nombre"] and r["resto"] else r["titular"]).lower()
                     for r in registros[1:] if r["titular"] != titular]
            extra = [e for e in dict.fromkeys(extra) if e and e not in titular.lower()][:2]

            sentido = describir_sentido(p["direccion"], p["carretera"], cfg)
            # Línea corta para el panel (sin mapa): "A-2 km 26 · saliendo de Madrid · Tráfico lento"
            corto = " · ".join(x for x in (f"{p['carretera']} {km_txt}".strip(), sentido, titular) if x)

            detalles = [sentido]
            inicio = min((r["inicio"] for r in registros if r["inicio"]), default=None)
            fin = max((r["fin"] for r in registros if r["fin"]), default=None)
            if fin:
                detalles.append(f"hasta {hora_local(fin, ahora)}")
            elif inicio:
                detalles.append(f"desde {hora_local(inicio, ahora)}")
            if p["lats"] and p["lons"]:
                detalles.append(enlace_mapa((p["lats"][0] + p["lats"][-1]) / 2,
                                            (p["lons"][0] + p["lons"][-1]) / 2))

            texto = f"{p['icono']} <b>{esc(lugar)}</b>\n{esc(titular)}"
            if extra:
                texto += f"\n<i>También: {esc('; '.join(extra))}</i>"
            texto += "\n" + " · ".join(d for d in detalles if d)

            rid = "dgt-" + (elem.get("id") or str(abs(hash(lugar + titular))))
            resultado[rid] = {
                "grupo": p["tramo"]["nombre"],
                "clase": p["clase"],
                "icono": p["icono"],
                "etiqueta": p["carretera"],
                "importante": any(r["avisa"] for r in registros),
                "corte_total": p["clase"] == "corte",
                "texto": texto,
                "corto": corto,
                "resumen": f"{lugar}: {titular}",
                "carretera": p["carretera"],
                "direccion": p["direccion"],
                "km": round(sum(kms) / len(kms), 1) if kms else None,
            }
        finally:
            elem.clear()
    return resultado


# ============================================================
# Ayuntamiento de Madrid: incidencias y sensores
# ============================================================

CORTE_TOTAL_MADRID = re.compile(r"corte total|cortad[ao] al trafico|calle cortada|cierre total|cerrad[ao] al trafico")
NIVELES_CALLE = {0: ("🟢", "fluido"), 1: ("🟡", "lento"), 2: ("🟠", "retenciones"), 3: ("🔴", "congestión")}


def calle_que_coincide(calles, descripcion):
    d = normalizar(descripcion)
    for calle, variantes in calles.items():
        if any(normalizar(v) in d for v in variantes):
            return calle
    return None


def resumir_descripcion(texto, maximo=160):
    t = re.sub(r"^\s*EXP[^/]*/\s*\d+\s*", "", texto or "")  # quita "EXP- 2015 / 242683"
    t = re.sub(r"\s+", " ", t).strip(" ./-")
    if t.isupper():
        t = t.capitalize()
    return t if len(t) <= maximo else t[:maximo - 1].rsplit(" ", 1)[0] + "…"


def incidencias_madrid(cfg, ahora):
    if not cfg["calles"]:
        return {}
    raiz = ET.fromstring(descargar(URL_MADRID))
    resultado = {}
    for inc in raiz.iter():
        if local(inc.tag) != "Incidencia":
            continue
        c = {local(e.tag): (e.text or "").strip() for e in inc}
        calle = calle_que_coincide(cfg["calles"], c.get("descripcion", ""))
        if not calle:
            continue
        inicio, fin = parse_fecha(c.get("fh_inicio")), parse_fecha(c.get("fh_final"))
        if c.get("incid_estado") != "1" and (not inicio or inicio > ahora):
            continue  # programada para más adelante
        if fin and fin < ahora:
            continue

        texto_norm = normalizar(c.get("descripcion", "") + " " + c.get("nom_tipo_incidencia", ""))
        corte_total = bool(CORTE_TOTAL_MADRID.search(texto_norm))
        es_obra = c.get("es_obras") == "S"
        es_accidente = c.get("es_accidente") == "S"
        clase = "corte" if corte_total else "accidente" if es_accidente else "obra" if es_obra else "otro"
        icono = CLASES[clase][1]
        tipo = c.get("nom_tipo_incidencia") or ("Obras" if es_obra else "Incidencia")

        detalles = []
        if fin:
            detalles.append(f"hasta {hora_local(fin, ahora)}")
        try:
            lat, lon = float(c.get("latitud", "nan")), float(c.get("longitud", "nan"))
            if lat == lat and lon == lon:  # no es NaN
                detalles.append(enlace_mapa(lat, lon))
        except ValueError:
            pass

        rid = "mad-" + (c.get("id_incidencia") or c.get("codigo") or str(abs(hash(c.get("descripcion")))))
        cuerpo = f"{icono} <b>{esc(calle)}</b> · {esc(tipo)}\n{esc(resumir_descripcion(c.get('descripcion')))}"
        if detalles:
            cuerpo += "\n" + " · ".join(detalles)
        resultado[rid] = {
            "grupo": "Calles de Madrid", "clase": clase, "icono": icono, "etiqueta": calle,
            "importante": clase != "obra", "corte_total": corte_total, "texto": cuerpo,
            "corto": f"{calle} · {tipo}", "resumen": f"{calle}: {tipo}",
            "carretera": "MADRID:" + calle, "direccion": "both", "km": None,
        }
    return resultado


def trafico_calles_madrid(cfg):
    """Nivel de tráfico actual de cada calle según los sensores del Ayuntamiento (sin avisos)."""
    if not cfg["calles"]:
        return {}
    niveles = {calle: [] for calle in cfg["calles"]}
    raiz = ET.fromstring(descargar(URL_SENSORES_MADRID))
    for pm in raiz.iter():
        if local(pm.tag) != "pm":
            continue
        d = {local(e.tag): (e.text or "").strip() for e in pm}
        calle = calle_que_coincide(cfg["calles"], d.get("descripcion", ""))
        if not calle or d.get("error", "N") not in ("N", ""):
            continue
        nivel = d.get("nivelServicio", "")
        if nivel.isdigit() and int(nivel) in NIVELES_CALLE:
            niveles[calle].append(int(nivel))
    resultado = {}
    for calle, lista in niveles.items():
        if not lista:
            continue
        lista.sort(reverse=True)
        # Con 3 o más sensores exigimos que al menos 2 coincidan, para ignorar un sensor suelto
        nivel = lista[1] if len(lista) >= 3 else lista[0]
        afectados = sum(1 for n in lista if n >= nivel) if nivel else 0
        resultado[calle] = {"nivel": nivel, "afectados": afectados, "total": len(lista)}
    return resultado


# ============================================================
# AEMET
# ============================================================

NIVELES_AEMET = {"verde": 0, "amarillo": 1, "naranja": 2, "rojo": 3}
NIVELES_METEOALARM = {"1": "verde", "2": "amarillo", "3": "naranja", "4": "rojo"}
# awareness_type de MeteoAlarm -> fenómeno
TIPOS_METEOALARM = {"2": "NE", "10": "PR", "12": "PR", "13": "PR", "3": "TO"}
FENOMENOS_AEMET = {"NE": ("❄️", "nevadas"), "PR": ("🌧️", "lluvias"), "TO": ("⛈️", "tormentas")}


def leer_meteoalarm(datos, cfg):
    """Avisos de MeteoAlarm (los de AEMET) que interesan y tocan las zonas de tus tramos."""
    umbral = {"NE": NIVELES_AEMET.get(cfg["avisos_meteo"]["nieve_desde"], 1)}
    lluvia = NIVELES_AEMET.get(cfg["avisos_meteo"]["lluvia_tormenta_desde"], 3)
    umbral.update({"PR": lluvia, "TO": lluvia})
    zonas_tramo = {t["carretera"]: {z.upper() for z in t.get("zonas_aviso", [])} for t in cfg["tramos"]}

    avisos = []
    for w in json.loads(datos).get("warnings", []):
        alerta = w.get("alert", {})
        if alerta.get("msgType") == "Cancel" or alerta.get("status", "Actual") != "Actual":
            continue
        for info in alerta.get("info", []):
            if not str(info.get("language", "es")).startswith("es"):
                continue
            p = {x.get("valueName"): str(x.get("value", "")) for x in info.get("parameter", [])}
            nivel = NIVELES_METEOALARM.get(p.get("awareness_level", "").split(";")[0].strip(), "verde")
            codigo = TIPOS_METEOALARM.get(p.get("awareness_type", "").split(";")[0].strip())
            if not codigo or NIVELES_AEMET[nivel] < max(umbral[codigo], 1):
                continue
            descripcion = re.sub(r"\s+", " ", info.get("description", "")).strip().rstrip(".")
            for area in info.get("area", []):
                zona = next((g.get("value", "") for g in area.get("geocode", [])
                             if g.get("valueName") == "EMMA_ID"), "").upper()
                afectados = [c for c, zonas in zonas_tramo.items() if zona and zona in zonas]
                if not afectados:
                    continue
                avisos.append({"zona": zona, "zona_nombre": area.get("areaDesc", ""), "codigo": codigo,
                               "nivel": nivel, "valor": descripcion[:120],
                               "onset": info.get("onset"), "expires": info.get("expires"),
                               "carreteras": afectados})
    return avisos


def avisos_aemet(cfg, estado, ahora):
    if not any(t.get("zonas_aviso") for t in cfg["tramos"]):
        return {}
    ultimo = estado.get("meteo_t")
    if not ultimo or ahora - datetime.fromisoformat(ultimo) >= CADA_METEO or "meteo_cache" not in estado:
        estado["meteo_cache"] = leer_meteoalarm(descargar(URL_METEOALARM), cfg)
        estado["meteo_t"] = ahora.isoformat()

    resultado = {}
    avisos = sorted(estado["meteo_cache"], key=lambda a: a["onset"] or "")
    for a in avisos:
        onset, expires = parse_fecha(a["onset"]), parse_fecha(a["expires"])
        if not expires or expires < ahora:
            continue
        rid = f"aemet-{a['zona']}-{a['codigo']}-{a['nivel']}"
        if rid in resultado:  # mismo aviso en varios días: nos quedamos con el primero
            continue
        icono, fenomeno = FENOMENOS_AEMET[a["codigo"]]
        n = NIVELES_AEMET[a["nivel"]]
        if not onset:
            cuando = f"hasta {dia_y_hora(expires, ahora)}"
        elif onset.astimezone(ZONA).date() == expires.astimezone(ZONA).date():
            cuando = f"{dia_y_hora(onset, ahora)}–{expires.astimezone(ZONA).strftime('%H:%M')}"
        else:
            cuando = f"{dia_y_hora(onset, ahora)} – {dia_y_hora(expires, ahora)}"
        carreteras = ", ".join(a["carreteras"])
        texto = (f"{icono}{CLASES['aemet' + str(n)][1]} <b>Aviso {a['nivel']} por {fenomeno}</b> · {esc(a['zona_nombre'])}\n"
                 + (f"{esc(a['valor'])} · " if a["valor"] else "") + f"{cuando}\nAfecta a: {esc(carreteras)}")
        resultado[rid] = {
            "grupo": "Avisos AEMET", "clase": f"aemet{n}", "icono": icono, "etiqueta": carreteras,
            "importante": True, "corte_total": n >= 3, "texto": texto,
            "corto": f"Aviso {a['nivel']} por {fenomeno} · {a['zona_nombre']}",
            "resumen": f"Aviso {a['nivel']} por {fenomeno} ({a['zona_nombre']})",
            "carretera": f"AEMET:{a['zona']}:{a['codigo']}", "direccion": "both", "km": None,
        }
    return resultado


# ============================================================
# Telegram
# ============================================================

def telegram(metodo, **params):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        if metodo != "getUpdates":
            print(f"[sin Telegram] {metodo}: {params.get('text', '')}\n")
        return [] if metodo == "getUpdates" else {"message_id": 1}
    datos = urllib.parse.urlencode(
        {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{metodo}", datos, timeout=30) as r:
            return json.loads(r.read()).get("result")
    except urllib.error.HTTPError as e:
        descripcion = json.loads(e.read() or b"{}").get("description", str(e))
        raise RuntimeError(f"Telegram {metodo}: {descripcion}") from None


def enviar(texto, silencioso=False):
    chat = os.environ.get("TELEGRAM_CHAT_ID", "0")
    trozos, actual = [], ""
    for bloque in texto.split("\n\n"):
        if actual and len(actual) + len(bloque) + 2 > 3900:
            trozos.append(actual)
            actual = ""
        actual += ("\n\n" if actual else "") + bloque
    if actual:
        trozos.append(actual)
    for t in trozos:
        telegram("sendMessage", chat_id=chat, text=t, parse_mode="HTML",
                 disable_web_page_preview="true", disable_notification=str(silencioso).lower())


BOTONES = {"p1d": timedelta(days=1), "p7d": timedelta(days=7)}


def procesar_botones(estado, ahora):
    """Lee las pulsaciones de los botones del panel desde la última revisión."""
    chat = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
    params = {"timeout": 0, "allowed_updates": ["callback_query"]}
    if estado.get("tg_offset"):
        params["offset"] = estado["tg_offset"]
    for u in telegram("getUpdates", **params) or []:
        estado["tg_offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if not cq or str(cq.get("message", {}).get("chat", {}).get("id")) != chat:
            continue  # solo atendemos tu chat
        accion = cq.get("data")
        if accion in BOTONES:
            estado["pausa_hasta"] = (ahora + BOTONES[accion]).isoformat()
            aviso = "Avisos en pausa"
        elif accion == "r":
            estado["pausa_hasta"] = None
            aviso = "Avisos reanudados"
        else:
            continue
        try:
            telegram("answerCallbackQuery", callback_query_id=cq["id"], text=aviso)
        except RuntimeError:
            pass  # la pulsación es antigua; da igual, el panel ya lo reflejará


def en_pausa(estado, ahora):
    hasta = estado.get("pausa_hasta")
    return bool(hasta) and ahora < datetime.fromisoformat(hasta)


# ============================================================
# Panel de estado fijado
# ============================================================

def linea_resumen(actuales, calles_trafico, ahora, pausado):
    """Primera línea del panel: es lo que se ve en la barra fijada sin abrir nada."""
    vistos, partes = set(), []
    for v in sorted((v for v in actuales.values() if v["importante"]), key=lambda v: CLASES[v["clase"]][0]):
        etiqueta = v.get("etiqueta") or v["grupo"]
        # Una entrada por carretera/calle (la más grave); los avisos AEMET van aparte
        clave = ("aemet", v["icono"], etiqueta) if v["grupo"] == "Avisos AEMET" else etiqueta
        if clave in vistos:
            continue
        vistos.add(clave)
        partes.append(f"{v['icono']} {esc(etiqueta)}")
    for calle, t in calles_trafico.items():
        if t["nivel"] >= 3:
            partes.append(f"🔴 {esc(calle)}")
    if len(partes) > MAX_EN_RESUMEN:
        partes = partes[:MAX_EN_RESUMEN] + ["…"]
    estado_txt = " · ".join(partes) if partes else "✅ Todo despejado"
    return f"🚦 <b>{hora_local(ahora, ahora, False)}</b> · " + ("⏸️ " if pausado else "") + estado_txt


def texto_panel(cfg, actuales, calles_trafico, ahora, pausa_hasta):
    grupos = [t["nombre"] for t in cfg["tramos"]]
    if cfg["calles"]:
        grupos.append("Calles de Madrid")
    if any(v["grupo"] == "Avisos AEMET" for v in actuales.values()):
        grupos.insert(0, "Avisos AEMET")

    cabecera = linea_resumen(actuales, calles_trafico, ahora, bool(pausa_hasta))
    if pausa_hasta:
        cabecera += f"\n<i>Avisos en pausa hasta {dia_y_hora(pausa_hasta, ahora)}. El panel sigue actualizándose.</i>"

    bloques = []
    for g in grupos:
        del_grupo = [v for v in actuales.values() if v["grupo"] == g]
        importantes = sorted((v for v in del_grupo if v["importante"]), key=lambda v: CLASES[v["clase"]][0])
        menores = sorted((v for v in del_grupo if not v["importante"]),
                         key=lambda v: (CLASES[v["clase"]][0], v.get("km") if v.get("km") is not None else 0))
        lineas_menores = list(dict.fromkeys(f"▫️ {esc(v.get('corto') or v['resumen'])}" for v in menores))
        if len(lineas_menores) > MAX_MENORES_POR_TRAMO:
            sobran = len(lineas_menores) - MAX_MENORES_POR_TRAMO
            lineas_menores = lineas_menores[:MAX_MENORES_POR_TRAMO] + [f"▫️ <i>y {sobran} más</i>"]

        # Tráfico de las calles de Madrid según los sensores
        linea_calles = ""
        if g == "Calles de Madrid" and calles_trafico:
            trozos = []
            for calle in cfg["calles"]:
                t = calles_trafico.get(calle)
                if t:
                    icono, palabra = NIVELES_CALLE[t["nivel"]]
                    trozos.append(f"{icono} {esc(calle)}" + (f" ({palabra})" if t["nivel"] else ""))
            linea_calles = "\n".join(trozos)

        if importantes:
            bloque = f"<b>{esc(g)}</b>\n" + "\n\n".join(v["texto"] for v in importantes)
            extra = "\n".join(x for x in (linea_calles, "\n".join(lineas_menores)) if x)
            if extra:
                bloque += "\n\n" + extra
        else:
            extra = "\n".join(x for x in (linea_calles, "\n".join(lineas_menores)) if x)
            bloque = f"✅ <b>{esc(g)}</b>" + ("\n" + extra if extra else "")
        bloques.append(bloque)

    texto = cabecera + "\n\n" + "\n\n".join(bloques)
    if len(texto) > LIMITE_PANEL:
        texto = texto[:LIMITE_PANEL - 60].rsplit("\n", 1)[0] + "\n\n<i>(recortado: hay demasiadas incidencias)</i>"
    return texto


def botones_panel(pausado):
    if pausado:
        return {"inline_keyboard": [[{"text": "▶️ Reanudar avisos", "callback_data": "r"}]]}
    return {"inline_keyboard": [[{"text": "⏸️ Pausar 1 día", "callback_data": "p1d"},
                                 {"text": "⏸️ Pausar 1 semana", "callback_data": "p7d"}]]}


def actualizar_panel(cfg, estado, actuales, calles_trafico, ahora):
    chat = os.environ.get("TELEGRAM_CHAT_ID", "0")
    pausa = datetime.fromisoformat(estado["pausa_hasta"]) if en_pausa(estado, ahora) else None
    texto = texto_panel(cfg, actuales, calles_trafico, ahora, pausa)
    teclado = botones_panel(bool(pausa))
    msg_id = estado.get("panel_id")
    if msg_id:
        try:
            telegram("editMessageText", chat_id=chat, message_id=msg_id, text=texto, parse_mode="HTML",
                     disable_web_page_preview="true", reply_markup=teclado)
            return
        except RuntimeError as e:
            if "not modified" in str(e):
                return
            # mensaje borrado o demasiado antiguo: creamos uno nuevo
    msg = telegram("sendMessage", chat_id=chat, text=texto, parse_mode="HTML",
                   disable_web_page_preview="true", disable_notification="true", reply_markup=teclado)
    estado["panel_id"] = msg["message_id"]
    try:
        telegram("pinChatMessage", chat_id=chat, message_id=msg["message_id"], disable_notification="true")
    except RuntimeError:
        pass


# ============================================================
# Estado y lógica principal
# ============================================================

def cargar_estado():
    try:
        with open(FICHERO_ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def guardar_estado(estado):
    with open(FICHERO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, sort_keys=True)


def huella(v, ahora):
    return {"resumen": v["resumen"], "clase": v["clase"], "corte": v["corte_total"],
            "carretera": v["carretera"], "direccion": v["direccion"], "km": v["km"],
            "t": ahora.isoformat()}


def es_repetida(v, recientes, ahora):
    """¿Se avisó hace poco de algo igual o más grave en el mismo punto?"""
    rango = CLASES[v["clase"]][0]
    for r in recientes:
        if ahora - datetime.fromisoformat(r["t"]) > VENTANA_REPETIDOS:
            continue
        if r["carretera"] != v["carretera"]:
            continue
        if "both" not in (r["direccion"], v["direccion"]) and r["direccion"] != v["direccion"]:
            continue
        if r["km"] is not None and v["km"] is not None and abs(r["km"] - v["km"]) > DISTANCIA_REPETIDOS_KM:
            continue
        if CLASES.get(r["clase"], (9,))[0] <= rango:
            return True
    return False


def main(ahora=None):
    cfg = cargar_config()
    ahora = ahora or datetime.now(timezone.utc)
    previo = cargar_estado() or {}
    version_previa = previo.get("version")
    estado = previo if version_previa == VERSION_ESTADO else {"panel_id": previo.get("panel_id")}
    previas = estado.get("incidencias", {})
    notificadas = estado.get("notificadas", {})
    ausencias = estado.get("ausencias", {})
    recientes = [r for r in estado.get("recientes", [])
                 if ahora - datetime.fromisoformat(r["t"]) <= VENTANA_REPETIDOS]
    errores = []

    try:
        procesar_botones(estado, ahora)
    except Exception as e:
        errores.append(f"Botones: {type(e).__name__}")
    pausado = en_pausa(estado, ahora)

    actuales = {}
    for nombre, funcion, prefijo in (("DGT", lambda: incidencias_dgt(cfg, ahora), "dgt-"),
                                     ("Ayuntamiento de Madrid", lambda: incidencias_madrid(cfg, ahora), "mad-"),
                                     ("AEMET", lambda: avisos_aemet(cfg, estado, ahora), "aemet-")):
        try:
            actuales.update(funcion())
        except Exception as e:  # si una fuente falla, mantenemos lo que sabíamos de ella
            errores.append(f"{nombre}: {type(e).__name__}")
            actuales.update({k: v for k, v in previas.items() if k.startswith(prefijo)})
    try:
        calles_trafico = trafico_calles_madrid(cfg)
    except Exception as e:
        errores.append(f"Sensores Madrid: {type(e).__name__}")
        calles_trafico = estado.get("calles_trafico", {})

    # En pausa no se deja nada pendiente: todo se da por avisado sin mensaje
    silencio = en_silencio(cfg, ahora) and not pausado
    importantes = {k: v for k, v in actuales.items() if v["importante"]}

    if version_previa != VERSION_ESTADO:
        # Arranque: no avisamos de lo que ya había; el panel lo muestra.
        notificadas = {k: huella(v, ahora) for k, v in importantes.items()}
        ausencias = {}
        if version_previa is None:
            enviar("🤖 <b>Bot de tráfico activado.</b>\nArriba tienes un panel fijado con el estado "
                   "de tus tramos; se actualiza solo y sin sonar.", silencioso=True)
    else:
        nuevas, empeoran = {}, {}
        for k, v in importantes.items():
            if k in notificadas:
                if v["corte_total"] and not notificadas[k].get("corte"):
                    empeoran[k] = v
                continue
            if silencio and not v["corte_total"]:
                continue  # se queda pendiente hasta que acabe el silencio
            if es_repetida(v, recientes, ahora):
                notificadas[k] = huella(v, ahora)  # lo damos por avisado, sin mensaje
                continue
            nuevas[k] = v

        bloques = []
        if nuevas:
            bloques.append(("🚨 <b>Nuevo aviso</b>" if len(nuevas) == 1
                            else f"🚨 <b>{len(nuevas)} avisos nuevos</b>"))
            bloques.extend(v["texto"] for v in sorted(nuevas.values(), key=lambda v: CLASES[v["clase"]][0]))
        if empeoran:
            bloques.append("⬆️ <b>Empeora</b>")
            bloques.extend(v["texto"] for v in empeoran.values())
        if bloques and not pausado:
            enviar("\n\n".join(bloques))
        for k, v in {**nuevas, **empeoran}.items():
            notificadas[k] = huella(v, ahora)
            recientes.append(notificadas[k])

        # Resueltas: solo tras varias revisiones seguidas sin aparecer, y fuera del silencio
        resueltas = []
        for k in list(notificadas):
            if k in actuales:
                ausencias.pop(k, None)
                continue
            if silencio:
                continue
            ausencias[k] = ausencias.get(k, 0) + 1
            if ausencias[k] >= AUSENCIAS_PARA_RESUELTO:
                info = notificadas.pop(k)
                ausencias.pop(k, None)
                if info["clase"] in CLASES_CON_RESUELTO:
                    resueltas.append(info["resumen"])
        if resueltas and not pausado:
            enviar("✅ <b>Resuelto</b>\n" + "\n".join("• " + esc(r) for r in resueltas), silencioso=True)

    # Errores persistentes (unos 30 min seguidos con revisión cada 5 min)
    fallos = estado.get("fallos_seguidos", 0) + 1 if errores else 0
    if fallos == 6 and not pausado:
        enviar("⚠️ No consigo leer alguna fuente de datos desde hace un rato:\n" + "\n".join(errores))

    estado.update({"version": VERSION_ESTADO, "incidencias": actuales, "notificadas": notificadas,
                   "ausencias": ausencias, "recientes": recientes, "fallos_seguidos": fallos,
                   "calles_trafico": calles_trafico})
    try:
        actualizar_panel(cfg, estado, actuales, calles_trafico, ahora)
    except RuntimeError as e:
        errores.append(str(e))
    guardar_estado(estado)

    # El registro de GitHub es público en un repositorio público: solo contamos, sin detalles.
    print(f"OK · {len(actuales)} incidencias ({len(importantes)} con aviso) · "
          f"silencio={'sí' if silencio else 'no'} · pausa={'sí' if pausado else 'no'} · errores={len(errores)}")
    for e in errores:
        print("  error:", e.split(":")[0])


if __name__ == "__main__":
    main()
