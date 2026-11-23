"""Cliente opcional de AEMET OpenData para corregir lluvia y mínimas de Open-Meteo."""
from datetime import datetime, timedelta
import math
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs):
        return False
import requests

from backend.config.settings import TARGET_REGION_BOUNDS

AEMET_BASE_URL = "https://opendata.aemet.es/opendata"


def get_aemet_api_key(explicit_key: Optional[str] = None) -> Optional[str]:
    """Devuelve AEMET_API_KEY desde entorno o .env local sin requerirla."""
    if explicit_key and explicit_key.strip():
        return explicit_key.strip()
    load_dotenv(override=False)
    value = os.getenv("AEMET_API_KEY", "").strip()
    return value or None


def parse_aemet_dms(coord_str: Optional[str]) -> Optional[float]:
    """Convierte coordenadas DMS de AEMET (p. ej. '404735N' o '040038W') a grados."""
    if not coord_str or not isinstance(coord_str, str):
        return None
    cleaned = coord_str.strip().upper()
    match = re.match(r"^(\d{6,7})([NSEW])$", cleaned)
    if not match:
        return None
    digits, hemi = match.groups()
    seconds = int(digits[-2:])
    minutes = int(digits[-4:-2])
    degrees = int(digits[:-4])
    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    if hemi in ("S", "W"):
        decimal = -decimal
    return round(decimal, 5)


def parse_aemet_float(raw: Any, allow_trace_precip: bool = False) -> Optional[float]:
    """Convierte valores numéricos con coma decimal o marcas especiales de AEMET."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if math.isfinite(val) else None
    text = str(raw).strip()
    if not text or text in ("-", "Varias", "Acum"):
        return None
    if text.lower() == "ip":
        return 0.1 if allow_trace_precip else None
    try:
        val = float(text.replace(",", "."))
        return val if math.isfinite(val) else None
    except ValueError:
        return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia aproximada en km entre dos coordenadas geográficas."""
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


class AemetOpenDataClient:
    """Cliente de dos pasos (HATEOAS) para la API pública AEMET OpenData."""

    def __init__(self, api_key: str, timeout_s: float = 25.0):
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update({
            "api_key": api_key,
            "User-Agent": "MapaSetero/1.0 (AEMET calibration)",
            "Accept": "application/json",
        })

    def _fetch_endpoint(self, path: str) -> Any:
        url = f"{AEMET_BASE_URL}{path}"
        for attempt in range(3):
            resp = self.session.get(url, timeout=self.timeout_s)
            if resp.status_code == 429:
                time.sleep(5.0 * (attempt + 1))
                continue
            resp.raise_for_status()
            meta = resp.json()
            estado = meta.get("estado")
            if estado == 404:
                return []
            if estado != 200 or not meta.get("datos"):
                raise RuntimeError(
                    f"AEMET API devolvió estado={estado}: {meta.get('descripcion')}"
                )
            data_resp = requests.get(meta["datos"], timeout=self.timeout_s)
            data_resp.raise_for_status()
            if not data_resp.encoding or data_resp.encoding.lower() == "iso-8859-1":
                data_resp.encoding = "latin-1"
            return data_resp.json()
        raise RuntimeError("AEMET OpenData superó los reintentos por límite de peticiones.")

    def fetch_regional_stations(self) -> Dict[str, Dict[str, Any]]:
        """Descarga inventario de estaciones dentro del área objetivo ampliada ±0.25°."""
        min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS
        rows = self._fetch_endpoint(
            "/valores/climatologicos/inventarioestaciones/todasestaciones"
        )
        stations: Dict[str, Dict[str, Any]] = {}
        for row in rows or []:
            code = row.get("indicativo")
            lat = parse_aemet_dms(row.get("latitud"))
            lon = parse_aemet_dms(row.get("longitud"))
            alt = parse_aemet_float(row.get("altitud"))
            if not code or lat is None or lon is None:
                continue
            if (min_lat - 0.25) <= lat <= (max_lat + 0.25) and (min_lon - 0.25) <= lon <= (max_lon + 0.25):
                stations[code] = {
                    "indicativo": code,
                    "nombre": row.get("nombre"),
                    "lat": lat,
                    "lon": lon,
                    "elevation_m": alt,
                }
        return stations

    def fetch_recent_daily_observations(
        self, days: int = 14, reference_date: Optional[str] = None
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Descarga valores climatológicos diarios por estación y fecha (YYYY-MM-DD)."""
        stations = self.fetch_regional_stations()
        if not stations:
            return {}
        end_d = (
            datetime.strptime(reference_date, "%Y-%m-%d").date()
            if reference_date
            else datetime.now(ZoneInfo("Europe/Madrid")).date()
        )
        start_d = end_d - timedelta(days=days)
        ini_str = f"{start_d.isoformat()}T00:00:00UTC"
        fin_str = f"{end_d.isoformat()}T23:59:59UTC"
        rows = self._fetch_endpoint(
            f"/valores/climatologicos/diarios/datos/fechaini/{ini_str}/fechafin/{fin_str}/todasestaciones"
        )
        by_station: Dict[str, Dict[str, Any]] = {}
        for row in rows or []:
            code = row.get("indicativo")
            fecha = row.get("fecha")
            if not code or code not in stations or not fecha:
                continue
            prec = parse_aemet_float(row.get("prec"), allow_trace_precip=True)
            tmin = parse_aemet_float(row.get("tmin"))
            tmed = parse_aemet_float(row.get("tmed"))
            entry = by_station.setdefault(
                code,
                {**stations[code], "days": {}},
            )
            entry["days"][fecha[:10]] = {
                "prec": prec,
                "tmin": tmin,
                "tmed": tmed,
            }

        # Complementar último día con observación convencional horaria (si aporta datos hoy)
        try:
            conv_rows = self._fetch_endpoint("/observacion/convencional/todas")
        except Exception:
            conv_rows = []
        today_iso = end_d.isoformat()
        for row in conv_rows or []:
            code = row.get("idema")
            fint = str(row.get("fint") or "")
            if not code or code not in stations or not fint.startswith(today_iso):
                continue
            entry = by_station.setdefault(code, {**stations[code], "days": {}})
            day_rec = entry["days"].setdefault(today_iso, {"prec": None, "tmin": None, "tmed": None})
            prec_h = parse_aemet_float(row.get("prec"), allow_trace_precip=True)
            ta_h = parse_aemet_float(row.get("ta"))
            tmin_h = parse_aemet_float(row.get("tmin"))
            if prec_h is not None:
                day_rec["prec"] = round((day_rec["prec"] or 0.0) + prec_h, 2)
            cand_min = tmin_h if tmin_h is not None else ta_h
            if cand_min is not None:
                day_rec["tmin"] = (
                    cand_min if day_rec["tmin"] is None else min(day_rec["tmin"], cand_min)
                )

        return {"stations": stations, "observations": by_station}


def _recompute_record_aggregates(rec: Dict[str, Any]) -> None:
    p14 = rec.get("precip_daily_mm")
    if isinstance(p14, list) and len(p14) == 14:
        rec["precip_last_3d_mm"] = round(sum(p14[-3:]), 2)
        rec["precip_accum_7d_mm"] = round(sum(p14[-7:]), 2)
        rec["precip_accum_10d_mm"] = round(sum(p14[-10:]), 2)
        rec["precip_accum_14d_mm"] = round(sum(p14), 2)
    p28 = rec.get("precip_daily_28d_mm")
    if isinstance(p28, list) and len(p28) == 28:
        rec["precip_accum_28d_mm"] = round(sum(p28), 2)
        rec["precip_15_28d_mm"] = round(sum(p28[:14]), 2)
    tmin28 = rec.get("temp_min_daily_28d_c")
    if isinstance(tmin28, list) and len(tmin28) == 28:
        rec["temp_min_14d_celsius"] = round(min(tmin28[-14:]), 2)
        rec["temp_min_recent_3d_celsius"] = round(min(tmin28[-3:]), 2)
        rec["frost_days_7d"] = int(sum(1 for t in tmin28[-7:] if t < 0.5))
        rec["frost_days_14d"] = int(sum(1 for t in tmin28[-14:] if t < 0.5))
        rec["hard_frost_28d"] = bool(min(tmin28) <= -1.0)


def calibrate_weather_records_with_aemet(
    records: List[Dict[str, Any]],
    cell_meta_by_h3: Dict[str, Tuple[float, float, Optional[float]]],
    snapshot_date: str,
    api_key: Optional[str] = None,
    client: Optional[AemetOpenDataClient] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Aplica corrección espacial suave con estaciones reales AEMET sobre Open-Meteo.

    Si no existe AEMET_API_KEY o falla la API, conserva intactos los registros Open-Meteo.
    """
    resolved_key = get_aemet_api_key(api_key)
    if not resolved_key and client is None:
        return records, {"aemet_calibrated": False, "aemet_stations_used": 0}

    try:
        aemet = client or AemetOpenDataClient(resolved_key)
        payload = aemet.fetch_recent_daily_observations(
            days=14, reference_date=snapshot_date
        )
    except Exception as exc:
        print(f"   [AEMET] Aviso: fallo al consultar AEMET OpenData ({exc}); se mantiene Open-Meteo sin calibrar.")
        return records, {"aemet_calibrated": False, "aemet_stations_used": 0}

    obs_by_station = payload.get("observations") if isinstance(payload, dict) else None
    if not obs_by_station:
        return records, {"aemet_calibrated": False, "aemet_stations_used": 0}

    end_d = datetime.strptime(snapshot_date, "%Y-%m-%d").date()
    day_index_by_iso = {
        (end_d - timedelta(days=13 - idx)).isoformat(): idx for idx in range(14)
    }

    records_by_h3 = {r["h3_index"]: r for r in records}
    station_residuals: List[Dict[str, Any]] = []

    for code, st in obs_by_station.items():
        st_lat, st_lon = st["lat"], st["lon"]
        st_elev = st.get("elevation_m")
        best_h3 = None
        best_dist = 9999.0
        for h3_id, (clat, clon, _) in cell_meta_by_h3.items():
            d = haversine_km(st_lat, st_lon, clat, clon)
            if d < best_dist:
                best_dist = d
                best_h3 = h3_id
        if best_h3 is None or best_dist > 25.0 or best_h3 not in records_by_h3:
            continue

        rec = records_by_h3[best_h3]
        p14_mod = rec.get("precip_daily_mm")
        tmin28_mod = rec.get("temp_min_daily_28d_c")
        tmin14_mod = tmin28_mod[-14:] if isinstance(tmin28_mod, list) and len(tmin28_mod) == 28 else None
        cell_elev = cell_meta_by_h3[best_h3][2]
        lapse_shift = 0.0
        if (
            isinstance(st_elev, (int, float))
            and isinstance(cell_elev, (int, float))
        ):
            lapse_shift = -0.006 * max(-500.0, min(500.0, float(cell_elev) - float(st_elev)))

        obs_rain_sum = 0.0
        mod_rain_sum = 0.0
        matched_rain_days = 0
        tmin_deltas: List[float] = []

        for day_iso, idx14 in day_index_by_iso.items():
            d_obs = st["days"].get(day_iso)
            if not d_obs:
                continue
            if (
                d_obs.get("prec") is not None
                and isinstance(p14_mod, list)
                and len(p14_mod) == 14
            ):
                obs_rain_sum += max(0.0, float(d_obs["prec"]))
                mod_rain_sum += max(0.0, float(p14_mod[idx14]))
                matched_rain_days += 1
            if d_obs.get("tmin") is not None and tmin14_mod is not None:
                obs_at_cell_elev = float(d_obs["tmin"]) + lapse_shift
                tmin_deltas.append(obs_at_cell_elev - float(tmin14_mod[idx14]))

        rain_ratio = None
        if matched_rain_days >= 5 and (obs_rain_sum >= 2.0 or mod_rain_sum >= 2.0):
            raw_ratio = (obs_rain_sum + 2.0) / (mod_rain_sum + 2.0)
            rain_ratio = max(0.65, min(1.55, raw_ratio))

        tmin_delta = None
        if len(tmin_deltas) >= 3:
            sorted_d = sorted(tmin_deltas)
            med_d = sorted_d[len(sorted_d) // 2]
            tmin_delta = max(-3.0, min(3.0, med_d))

        if rain_ratio is not None or tmin_delta is not None:
            station_residuals.append({
                "code": code,
                "lat": st_lat,
                "lon": st_lon,
                "rain_ratio": rain_ratio,
                "tmin_delta": tmin_delta,
            })

    if not station_residuals:
        return records, {"aemet_calibrated": False, "aemet_stations_used": 0}

    sigma_km = 28.0
    max_radius_km = 65.0
    calibrated_count = 0

    for rec in records:
        meta = cell_meta_by_h3.get(rec["h3_index"])
        if meta is None:
            rec["aemet_calibrated"] = False
            rec["aemet_stations_used"] = len(station_residuals)
            continue
        clat, clon, _ = meta
        r_num = 0.0
        r_den = 0.0
        t_num = 0.0
        t_den = 0.0
        for sr in station_residuals:
            d = haversine_km(clat, clon, sr["lat"], sr["lon"])
            if d > max_radius_km:
                continue
            w = math.exp(-0.5 * (d / sigma_km) ** 2)
            if sr["rain_ratio"] is not None:
                r_num += sr["rain_ratio"] * w
                r_den += w
            if sr["tmin_delta"] is not None:
                t_num += sr["tmin_delta"] * w
                t_den += w

        applied = False
        if r_den > 0:
            blend = min(1.0, r_den / (r_den + 0.35))
            ratio = 1.0 + ((r_num / r_den) - 1.0) * blend
            if abs(ratio - 1.0) >= 0.01:
                if isinstance(rec.get("precip_daily_mm"), list):
                    rec["precip_daily_mm"] = [round(v * ratio, 2) for v in rec["precip_daily_mm"]]
                if isinstance(rec.get("precip_daily_28d_mm"), list):
                    # Aplicar corrección más fuerte en la ventana reciente de 14d
                    p28 = rec["precip_daily_28d_mm"]
                    rec["precip_daily_28d_mm"] = [
                        round(v * (1.0 + (ratio - 1.0) * (0.45 if idx < 14 else 1.0)), 2)
                        for idx, v in enumerate(p28)
                    ]
                else:
                    rec["precip_accum_14d_mm"] = round(rec["precip_accum_14d_mm"] * ratio, 2)
                applied = True

        if t_den > 0:
            blend = min(1.0, t_den / (t_den + 0.35))
            delta = (t_num / t_den) * blend
            if abs(delta) >= 0.05:
                if isinstance(rec.get("temp_min_daily_28d_c"), list):
                    rec["temp_min_daily_28d_c"] = [
                        round(v + (delta * 0.45 if idx < 14 else delta), 2)
                        for idx, v in enumerate(rec["temp_min_daily_28d_c"])
                    ]
                else:
                    rec["temp_min_14d_celsius"] = round(rec["temp_min_14d_celsius"] + delta, 2)
                    if rec.get("temp_min_recent_3d_celsius") is not None:
                        rec["temp_min_recent_3d_celsius"] = round(
                            rec["temp_min_recent_3d_celsius"] + delta, 2
                        )
                applied = True

        _recompute_record_aggregates(rec)
        rec["aemet_calibrated"] = applied
        rec["aemet_stations_used"] = len(station_residuals)
        if applied:
            calibrated_count += 1

    print(
        f"[AEMET] Calibradas {calibrated_count}/{len(records)} celdas con "
        f"{len(station_residuals)} estaciones reales."
    )
    return records, {
        "aemet_calibrated": calibrated_count > 0,
        "aemet_stations_used": len(station_residuals),
    }
