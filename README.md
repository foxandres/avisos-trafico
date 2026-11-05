# 🚦 Bot de avisos de tráfico (Telegram)

Bot que vigila las incidencias oficiales de la **DGT** (carreteras) y del **Ayuntamiento de Madrid** (calles) y avisa por Telegram.

- **Avisa solo de lo importante:** cortes, accidentes, retenciones, meteorología y eventos. Las obras que no cortan la vía no avisan; solo se cuentan en el panel.
- **Panel de estado fijado** en el chat, actualizado sin notificaciones cada pocos minutos.
- **Horario de silencio** configurable: durante él solo avisan los cortes totales y el resto llega al terminar.
- Avisa cuando se **resuelve** algo de lo que te avisó.
- Enlace a **Google Maps** en cada incidencia.

Los tramos y calles que vigila **no están en el código**: se guardan en el secret `CONFIG`. El estado se guarda en la caché de GitHub Actions, no en el repositorio.

## Configuración

Crea tres secrets en **Settings → Secrets and variables → Actions**:

| Secret | Contenido |
|---|---|
| `TELEGRAM_TOKEN` | token del bot (de @BotFather) |
| `TELEGRAM_CHAT_ID` | tu chat ID |
| `CONFIG` | JSON con tus tramos y calles (formato en `config.ejemplo.json`) |

### Formato de `CONFIG`

- `tramos`: cada tramo puede filtrarse por:
  - **kilómetros:** `"km_min"` y `"km_max"`
  - **zona geográfica:** `"lat_min"`, `"lat_max"`, `"lon_min"`, `"lon_max"`, útil cuando el kilometraje cambia por provincias
  - **longitud:** solo `"lon_min"` y `"lon_max"`
- `calles`: nombre de la calle y las formas en que puede aparecer escrita (sin importar mayúsculas ni tildes).
- `silencio_utc`: `[inicio, fin]` en horas UTC. España va 1 hora por delante en invierno y 2 en verano. Usa `null` para no tener silencio.
- `hacia_madrid_negativo`: carreteras radiales cuyos kilómetros bajan hacia Madrid, para que el sentido salga como "hacia Madrid" o "saliendo de Madrid".

## Funcionamiento

GitHub Actions ejecuta `bot.py` cada 5 minutos, gratis en repositorios públicos. Para lanzarlo a mano: **Actions → Avisos de tráfico → Run workflow**.

Para pausarlo: **Actions → Avisos de tráfico → ··· → Disable workflow**.

Datos: DGT (Punto de Acceso Nacional) y Portal de Datos Abiertos del Ayuntamiento de Madrid, licencia CC BY.
