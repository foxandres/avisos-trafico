#!/usr/bin/env python3
"""
Bot de avisos de tráfico -> Telegram

Fuentes oficiales:
  - DGT (Punto de Acceso Nacional): incidencias en carreteras.
  - Ayuntamiento de Madrid: incidencias en calles de la ciudad.

Qué hace en cada ejecución:
  1. Descarga las incidencias y se queda con las de tus tramos y calles.
  2. Te avisa de las IMPORTANTES nuevas (cortes, accidentes, retenciones, meteorología…).
     Las obras que no cortan la vía no avisan: solo se cuentan en el panel.
  3. Avisa cuando una incidencia de la que te avisó desaparece.
  4. En horario de silencio solo avisa de cortes totales; el resto llega al acabar el silencio.
  5. Mantiene un panel de estado fijado en el chat, actualizado sin notificaciones.

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
from datetime import datetime, timezone

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


def hora_local(dt, con_dia_si_no_es_hoy=True):
    if not dt:
        return ""
    dt = dt.astimezone(ZONA)
    hoy = datetime.now(ZONA).date()
    if con_dia_si_no_es_hoy and dt.date() != hoy:
        return dt.strftime("%d/%m %H:%M")
    return dt.strftime("%H:%M")


def esc(texto):
    return (texto or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def enlace_mapa(lat, lon):
    return f'<a href="https://maps.google.com/?q={lat:.5f},{lon:.5f}">Ver mapa</a>'


def descargar(url):
    req = urllib.request.Request(url, headers={"User-Agent": "avisos-trafico-bot/2.0"})
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
# Textos de la DGT
# ============================================================

CAUSAS = {
    "accident": ("💥", "Accidente"),
    "abnormalTraffic": ("🚗", "Retención"),
    "vehicleObstruction": ("⚠️", "Vehículo detenido"),
    "obstruction": ("⚠️", "Obstáculo en la vía"),
    "generalObstruction": ("⚠️", "Obstáculo en la vía"),
    "animalPresenceObstruction": ("⚠️", "Animales en la vía"),
    "environmentalObstruction": ("⚠️", "Obstáculo en la vía"),
    "infrastructureDamageObstruction": ("⚠️", "Daños en la vía"),
    "poorEnvironment": ("🌧️", "Meteorología adversa"),
    "poorRoadInfrastructure": ("⚠️", "Problema en la vía"),
    "publicEvent": ("⚠️", "Evento"),
    "fire": ("🔥", "Incendio"),
    "roadMaintenance": ("🚧", "Obras"),
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

METEO = {
    "snowfall": ("❄️", "Nieve"), "ice": ("🧊", "Hielo"), "blackIce": ("🧊", "Hielo"),
    "fog": ("🌫️", "Niebla"), "denseFog": ("🌫️", "Niebla densa"),
    "heavyRain": ("🌧️", "Lluvia intensa"), "strongWinds": ("💨", "Viento fuerte"),
    "flooding": ("🌊", "Inundación"), "snowOnTheRoad": ("❄️", "Nieve en la calzada"),
}

TRAFICO = {
    "stationaryTraffic": "tráfico detenido", "queuingTraffic": "colas",
    "slowTraffic": "tráfico lento", "heavyTraffic": "tráfico denso",
}

CIERRES_TOTALES = {"roadClosed", "carriagewayClosures"}
CIERRES = CIERRES_TOTALES | {"exitClosed", "entryClosed", "contraflow"}

EFECTOS = {
    "roadClosed": "carretera cortada",
    "carriagewayClosures": "calzada cortada",
    "exitClosed": "salida cerrada",
    "entryClosed": "entrada cerrada",
    "contraflow": "circulación por el carril contrario",
    "narrowLanes": "carriles estrechos",
    "singleAlternateLineTraffic": "paso alternativo",
    "lanesDeviated": "carriles desviados",
    "newRoadworksLayout": "cambio de trazado",
    "hardShoulderClosed": "arcén cortado",
}

CARRILES = {
    "allLanesCompleteCarriageway": "todos los carriles",
    "leftLane": "carril izquierdo", "rightLane": "carril derecho",
    "middleLane": "carril central", "rightHardShoulder": "arcén derecho",
    "leftHardShoulder": "arcén izquierdo",
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


def describir_sentido(campos, carretera, cfg):
    d = (campos.get("tpegDirectionRoad") or ["unknown"])[0]
    if d == "both":
        return "ambos sentidos"
    if carretera in cfg["hacia_madrid_negativo"]:
        return {"positive": "saliendo de Madrid", "negative": "hacia Madrid"}.get(d, "")
    return {"positive": "km crecientes", "negative": "km decrecientes"}.get(d, "")


def describir_dgt(elem, campos):
    """Devuelve (icono, titular, importante, corte_total)."""
    causa = TIPOS_REGISTRO.get(elem.get(XSI_TYPE, "")) or (campos.get("causeType") or [""])[0]
    gestion = (campos.get("roadOrCarriagewayOrLaneManagementType") or [""])[0]
    carriles = [CARRILES.get(c, c) for c in dict.fromkeys(campos.get("laneUsage", []))]

    icono, nombre_causa = CAUSAS.get(causa, ("⚠️", "Incidencia"))
    for v in campos.get("poorEnvironmentType", []) + campos.get("weatherRelatedRoadConditionType", []):
        if v in METEO:
            icono, nombre_causa = METEO[v]
            break
    detalle_trafico = next((TRAFICO[v] for v in campos.get("abnormalTrafficType", []) if v in TRAFICO), "")

    if gestion == "laneClosures":
        efecto = (("todos los carriles cortados" if carriles == ["todos los carriles"]
                   else f"{', '.join(carriles)} cortado") if carriles else "carril cortado")
    else:
        efecto = EFECTOS.get(gestion, "")
    if gestion == "laneClosures" and carriles == ["todos los carriles"]:
        gestion = "carriagewayClosures"

    corte_total = gestion in CIERRES_TOTALES
    es_obra = causa in ("roadMaintenance", "") and not detalle_trafico
    importante = (gestion in CIERRES) or not es_obra
    if corte_total:
        icono = "🚫"

    partes = [nombre_causa if causa or not efecto else ""]
    if detalle_trafico:
        partes.append(detalle_trafico)
    if efecto:
        partes.append(efecto)
    titular = " · ".join(p for p in partes if p)
    titular = titular[:1].upper() + titular[1:] if titular else "Incidencia"
    return icono, titular, importante, corte_total


def incidencias_dgt(cfg, ahora):
    tramos = cfg["tramos"]
    carreteras = {t["carretera"] for t in tramos}
    if not carreteras:
        return {}
    resultado = {}
    for _ev, elem in ET.iterparse(io.BytesIO(descargar(URL_DGT)), events=("end",)):
        if local(elem.tag) != "situationRecord":
            continue
        try:
            campos = campos_registro(elem)
            carretera = (campos.get("roadName") or [""])[0].upper().replace(" ", "")
            if carretera not in carreteras:
                continue
            kms = [float(k) for k in campos.get("kilometerPoint", []) if NUM.match(k)]
            lons = [float(x) for x in campos.get("longitude", []) if NUM.match(x)]
            lats = [float(x) for x in campos.get("latitude", []) if NUM.match(x)]
            tramo = tramo_que_coincide(tramos, carretera, kms, lons, lats)
            if not tramo:
                continue
            if (campos.get("validityStatus") or ["active"])[0] not in ("active", "planned"):
                continue
            inicio = parse_fecha((campos.get("overallStartTime") or [""])[0])
            fin = parse_fecha((campos.get("overallEndTime") or [""])[0])
            if inicio and inicio > ahora:
                continue  # todavía no ha empezado
            if fin and fin < ahora:
                continue

            icono, titular, importante, corte_total = describir_dgt(elem, campos)
            km_txt = ""
            if kms:
                km_txt = f"km {min(kms):g}" if min(kms) == max(kms) else f"km {min(kms):g}–{max(kms):g}"
            municipios = list(dict.fromkeys(campos.get("municipality", [])))
            lugar = " · ".join(p for p in (f"{carretera} {km_txt}".strip(), ", ".join(municipios[:2])) if p)

            detalles = [describir_sentido(campos, carretera, cfg)]
            if fin:
                detalles.append(f"hasta {hora_local(fin)}")
            elif inicio:
                detalles.append(f"desde {hora_local(inicio)}")
            if lats and lons:
                detalles.append(enlace_mapa((lats[0] + lats[-1]) / 2, (lons[0] + lons[-1]) / 2))

            rid = "dgt-" + (elem.get("id") or str(abs(hash(str(sorted(campos.items()))))))
            resultado[rid] = {
                "grupo": tramo["nombre"],
                "icono": icono,
                "importante": importante,
                "corte_total": corte_total,
                "texto": (f"{icono} <b>{esc(lugar)}</b>\n{esc(titular)}\n"
                          + " · ".join(d for d in detalles if d)),
                "resumen": f"{lugar}: {titular}",
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
        importante = corte_total or not es_obra
        icono = "🚫" if corte_total else "💥" if es_accidente else "🚧" if es_obra else "⚠️"
        tipo = c.get("nom_tipo_incidencia") or ("Obras" if es_obra else "Incidencia")

        detalles = []
        if fin:
            detalles.append(f"hasta {hora_local(fin)}")
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
            "grupo": "Calles de Madrid", "icono": icono, "importante": importante,
            "corte_total": corte_total, "texto": cuerpo, "resumen": f"{calle}: {tipo}",
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
    lineas = [f"🚦 <b>Estado del tráfico</b> · {hora_local(ahora, False)}"]
    obras_menores = {}
    for g in grupos:
        del_grupo = [v for v in actuales.values() if v["grupo"] == g]
        importantes = [v for v in del_grupo if v["importante"]]
        menores = len(del_grupo) - len(importantes)
        if menores:
            obras_menores[g] = menores
        if not importantes:
            lineas.append(f"\n✅ {esc(g)}")
            continue
        lineas.append(f"\n<b>{esc(g)}</b>\n" + "\n\n".join(v["texto"] for v in importantes))
    if obras_menores:
        total = sum(obras_menores.values())
        lineas.append(f"\n🚧 {total} obra{'s' if total > 1 else ''} sin cortes de vía "
                      f"({', '.join(esc(g) for g in obras_menores)})")
    lineas.append("\n<i>Se actualiza solo cada pocos minutos.</i>")
    return "\n".join(lineas)


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


def main(ahora=None):
    cfg = cargar_config()
    ahora = ahora or datetime.now(timezone.utc)
    estado_previo = cargar_estado()
    primera_vez = estado_previo is None or estado_previo.get("version") != 2
    estado = estado_previo if not primera_vez else {}
    previas = estado.get("incidencias", {})
    notificadas = estado.get("notificadas", {})

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

    if primera_vez:
        # Arranque: no avisamos de lo que ya había; el panel lo muestra todo.
        notificadas = {k: v["resumen"] for k, v in importantes.items()}
        enviar("🤖 <b>Bot de tráfico actualizado.</b>\n"
               "Te he dejado arriba un panel fijado con el estado de tus tramos: "
               "se actualiza solo y sin hacer ruido. Ábrelo antes de salir.\n\n"
               "A partir de ahora solo te avisaré de cortes, accidentes, retenciones y meteorología "
               "(las obras que no cortan la vía no avisan).", silencioso=True)
    else:
        a_avisar = {k: v for k, v in importantes.items()
                    if k not in notificadas and (not silencio or v["corte_total"])}
        resueltas = {k: r for k, r in notificadas.items() if k not in actuales}
        if a_avisar:
            cabecera = "🚨 <b>Nueva incidencia</b>" if len(a_avisar) == 1 else f"🚨 <b>{len(a_avisar)} incidencias nuevas</b>"
            enviar(cabecera + "\n\n" + "\n\n".join(v["texto"] for v in a_avisar.values()))
            notificadas.update({k: v["resumen"] for k, v in a_avisar.items()})
        if resueltas and not silencio:
            enviar("✅ <b>Resuelto</b>\n" + "\n".join("• " + esc(r) for r in resueltas.values()), silencioso=True)
            for k in resueltas:
                notificadas.pop(k, None)

    # Errores persistentes (unos 30 min seguidos con revisión cada 5 min)
    fallos = estado.get("fallos_seguidos", 0) + 1 if errores else 0
    if fallos == 6:
        enviar("⚠️ No consigo leer alguna fuente de datos desde hace un rato:\n" + "\n".join(errores))

    estado.update({"version": 2, "incidencias": actuales, "notificadas": notificadas,
                   "fallos_seguidos": fallos})
    try:
        actualizar_panel(cfg, estado, actuales, ahora)
    except RuntimeError as e:
        errores.append(str(e))
    guardar_estado(estado)

    # El registro de GitHub es público en un repositorio público: solo contamos, sin detalles.
    print(f"OK · {len(actuales)} incidencias ({len(importantes)} importantes) · "
          f"silencio={'sí' if silencio else 'no'} · errores={len(errores)}")
    for e in errores:
        print("  error:", e.split(":")[0])


if __name__ == "__main__":
    main()
