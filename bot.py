#!/usr/bin/env python3
"""
Bot de avisos de tráfico -> Telegram  (v3)

Fuentes oficiales:
  - DGT (Punto de Acceso Nacional): incidencias en carreteras.
  - Ayuntamiento de Madrid: incidencias en calles de la ciudad.

Qué te AVISA (con notificación):
  - Cortes de carretera o calzada, accidentes, meteorología adversa, retenciones con tráfico
    detenido o colas, salidas/entradas cerradas.
  - Si algo que ya te avisó empeora a corte total.
  - Cuando se resuelve un corte o accidente del que te avisó (sin sonido).

Qué NO avisa (aparece en el panel fijado, una línea corta cada una):
  - Obras sin corte, vehículos detenidos, obstáculos, tráfico lento o denso.

Para no repetir:
  - Una incidencia de la DGT con varias partes (corte + desvío + retención) es UN solo aviso.
  - Algo "resuelto" tiene que llevar 15 min sin aparecer.
  - Si aparece otra incidencia del mismo tipo en el mismo punto (±2 km, mismo sentido)
    en la última hora, no se vuelve a avisar.
  - En horario de silencio solo avisan los cortes totales; lo demás llega al acabar.

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
FICHERO_ESTADO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "estado.json")
XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
NUM = re.compile(r"^-?\d+(\.\d+)?$")
VERSION_ESTADO = 3

AUSENCIAS_PARA_RESUELTO = 3        # revisiones seguidas sin aparecer (~15 min)
VENTANA_REPETIDOS = timedelta(minutes=60)
DISTANCIA_REPETIDOS_KM = 2.0
CLASES_CON_RESUELTO = {"corte", "accidente"}


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


def fkm(k):
    return f"{round(k, 1):g}".replace(".", ",")


def esc(texto):
    return (texto or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def enlace_mapa(lat, lon):
    return f'<a href="https://maps.google.com/?q={lat:.5f},{lon:.5f}">Ver mapa</a>'


def descargar(url):
    req = urllib.request.Request(url, headers={"User-Agent": "avisos-trafico-bot/3.0"})
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
    "accidente": (1, "💥", True),
    "meteo":     (2, "🌧️", True),
    "retencion": (3, "🚗", True),
    "cierre":    (4, "⛔", True),
    "otro":      (4, "⚠️", True),
    "lento":     (5, "🚗", False),
    "vehiculo":  (6, "⚠️", False),
    "obra":      (7, "🚧", False),
}

MAX_MENORES_POR_TRAMO = 6
LIMITE_PANEL = 3900  # Telegram admite 4096 caracteres por mensaje


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


def analizar_registro(elem, campos):
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
        info = analizar_registro(rec, campos)
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
            mapa = ""
            if p["lats"] and p["lons"]:
                mapa = (f'https://maps.google.com/?q={(p["lats"][0] + p["lats"][-1]) / 2:.5f},'
                        f'{(p["lons"][0] + p["lons"][-1]) / 2:.5f}')
            # Línea corta para el panel (sin mapa): "A-2 km 26 · saliendo de Madrid · Tráfico lento"
            breve = p["resto"] if p["clase"] == "lento" and p["resto"] else titular
            corto = " · ".join(x for x in (f"{p['carretera']} {km_txt}".strip(), sentido,
                                           breve[:1].upper() + breve[1:]) if x)
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
                "importante": any(r["avisa"] for r in registros),
                "corte_total": p["clase"] == "corte",
                "texto": texto,
                "resumen": f"{lugar}: {titular}",
                "corto": corto,
                "mapa": mapa,
                "carretera": p["carretera"],
                "direccion": p["direccion"],
                "km": round(sum(kms) / len(kms), 1) if kms else None,
            }
        finally:
            elem.clear()
    return resultado


# ============================================================
# Ayuntamiento de Madrid
# ============================================================

CORTE_TOTAL_MADRID = re.compile(r"corte total|cortad[ao] al trafico|calle cortada|cierre total|cerrad[ao] al trafico")


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

        detalles, mapa = [], ""
        if fin:
            detalles.append(f"hasta {hora_local(fin, ahora)}")
        try:
            lat, lon = float(c.get("latitud", "nan")), float(c.get("longitud", "nan"))
            if lat == lat and lon == lon:  # no es NaN
                detalles.append(enlace_mapa(lat, lon))
                mapa = f"https://maps.google.com/?q={lat:.5f},{lon:.5f}"
        except ValueError:
            pass
        corto = f"{calle} · {tipo}"

        rid = "mad-" + (c.get("id_incidencia") or c.get("codigo") or str(abs(hash(c.get("descripcion")))))
        cuerpo = f"{icono} <b>{esc(calle)}</b> · {esc(tipo)}\n{esc(resumir_descripcion(c.get('descripcion')))}"
        if detalles:
            cuerpo += "\n" + " · ".join(detalles)
        resultado[rid] = {
            "grupo": "Calles de Madrid", "clase": clase, "importante": clase != "obra",
            "corte_total": corte_total, "texto": cuerpo, "resumen": f"{calle}: {tipo}",
            "corto": corto, "mapa": mapa,
            "carretera": "MADRID:" + calle, "direccion": "both", "km": None,
        }
    return resultado


# ============================================================
# Telegram
# ============================================================

def telegram(metodo, **params):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print(f"[sin Telegram] {metodo}: {params.get('text', '')}\n")
        return {"message_id": 1}
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


# ============================================================
# Panel de estado fijado
# ============================================================

def texto_panel(cfg, actuales, ahora):
    grupos = [t["nombre"] for t in cfg["tramos"]]
    if cfg["calles"]:
        grupos.append("Calles de Madrid")
    cabecera = f"🚦 <b>Estado del tráfico</b> · {hora_local(ahora, ahora, False)}"
    bloques = []
    for g in grupos:
        del_grupo = [v for v in actuales.values() if v["grupo"] == g]
        importantes = sorted((v for v in del_grupo if v["importante"]), key=lambda v: CLASES[v["clase"]][0])
        # Sin aviso: una línea corta cada una, por tipo y kilómetro
        menores = sorted((v for v in del_grupo if not v["importante"]),
                         key=lambda v: (CLASES[v["clase"]][0], v.get("km") if v.get("km") is not None else 0))
        lineas_menores = list(dict.fromkeys(f"▫️ {esc(v.get('corto') or v['resumen'])}" for v in menores))
        if len(lineas_menores) > MAX_MENORES_POR_TRAMO:
            sobran = len(lineas_menores) - MAX_MENORES_POR_TRAMO
            lineas_menores = lineas_menores[:MAX_MENORES_POR_TRAMO] + [f"▫️ <i>y {sobran} más</i>"]

        if importantes:
            bloque = f"<b>{esc(g)}</b>\n" + "\n\n".join(v["texto"] for v in importantes)
            if lineas_menores:
                bloque += "\n\n" + "\n".join(lineas_menores)
        else:
            bloque = f"✅ <b>{esc(g)}</b>" + ("\n" + "\n".join(lineas_menores) if lineas_menores else "")
        bloques.append(bloque)

    texto = cabecera + "\n\n" + "\n\n".join(bloques)
    if len(texto) > LIMITE_PANEL:
        texto = texto[:LIMITE_PANEL - 60].rsplit("\n", 1)[0] + "\n\n<i>(recortado: hay demasiadas incidencias)</i>"
    return texto


def actualizar_panel(cfg, estado, actuales, ahora):
    chat = os.environ.get("TELEGRAM_CHAT_ID", "0")
    texto = texto_panel(cfg, actuales, ahora)
    msg_id = estado.get("panel_id")
    if msg_id:
        try:
            telegram("editMessageText", chat_id=chat, message_id=msg_id, text=texto,
                     parse_mode="HTML", disable_web_page_preview="true")
            return
        except RuntimeError as e:
            if "not modified" in str(e):
                return
            # mensaje borrado o demasiado antiguo: creamos uno nuevo
    msg = telegram("sendMessage", chat_id=chat, text=texto, parse_mode="HTML",
                   disable_web_page_preview="true", disable_notification="true")
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
        if CLASES[r["clase"]][0] <= rango:
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

    actuales, errores = {}, []
    for nombre, funcion, prefijo in (("DGT", incidencias_dgt, "dgt-"),
                                     ("Ayuntamiento de Madrid", incidencias_madrid, "mad-")):
        try:
            actuales.update(funcion(cfg, ahora))
        except Exception as e:  # si una fuente falla, mantenemos lo que sabíamos de ella
            errores.append(f"{nombre}: {type(e).__name__}")
            actuales.update({k: v for k, v in previas.items() if k.startswith(prefijo)})

    silencio = en_silencio(cfg, ahora)
    importantes = {k: v for k, v in actuales.items() if v["importante"]}

    if version_previa != VERSION_ESTADO:
        # Arranque o actualización: no avisamos de lo que ya había; el panel lo muestra.
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
            bloques.append(("🚨 <b>Nueva incidencia</b>" if len(nuevas) == 1
                            else f"🚨 <b>{len(nuevas)} incidencias nuevas</b>"))
            bloques.extend(v["texto"] for v in sorted(nuevas.values(), key=lambda v: CLASES[v["clase"]][0]))
        if empeoran:
            bloques.append("⬆️ <b>Empeora: ahora hay corte</b>")
            bloques.extend(v["texto"] for v in empeoran.values())
        if bloques:
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
        if resueltas:
            enviar("✅ <b>Resuelto</b>\n" + "\n".join("• " + esc(r) for r in resueltas), silencioso=True)

    # Errores persistentes (unos 30 min seguidos con revisión cada 5 min)
    fallos = estado.get("fallos_seguidos", 0) + 1 if errores else 0
    if fallos == 6:
        enviar("⚠️ No consigo leer alguna fuente de datos desde hace un rato:\n" + "\n".join(errores))

    estado.update({"version": VERSION_ESTADO, "incidencias": actuales, "notificadas": notificadas,
                   "ausencias": ausencias, "recientes": recientes, "fallos_seguidos": fallos})
    try:
        actualizar_panel(cfg, estado, actuales, ahora)
    except RuntimeError as e:
        errores.append(str(e))
    guardar_estado(estado)

    # El registro de GitHub es público en un repositorio público: solo contamos, sin detalles.
    print(f"OK · {len(actuales)} incidencias ({len(importantes)} con aviso) · "
          f"silencio={'sí' if silencio else 'no'} · errores={len(errores)}")
    for e in errores:
        print("  error:", e.split(":")[0])


if __name__ == "__main__":
    main()
