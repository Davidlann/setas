"""Cliente Open-Meteo con interpolación espacial de la malla meteorológica."""
from typing import List, Dict, Any, Tuple
import requests
import time
from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo

WEATHER_GRID_STEP_DEG = 0.1
WEATHER_INTERPOLATION_METHOD = "idw_4_nearest"
WEATHER_SOURCE = (
    "Open-Meteo Forecast API; Best Match selecciona el modelo automáticamente"
)


class OpenMeteoClient:
    BASE_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, request_delay_seconds: float = 0.8):
        self.session = requests.Session()
        self.request_delay = request_delay_seconds

    @staticmethod
    def build_forest_covering_grid(
        cell_coords: List[Tuple[float, float]],
        grid_step_deg: float = WEATHER_GRID_STEP_DEG,
    ) -> List[Tuple[float, float]]:
        """Genera únicamente los nodos 0.1° necesarios para cubrir las celdas forestales."""
        if not cell_coords:
            return []
        lat0 = round(min(lat for lat, _ in cell_coords) - grid_step_deg, 3)
        lon0 = round(min(lon for _, lon in cell_coords) - grid_step_deg, 3)
        needed = set()
        for clat, clon in cell_coords:
            i = math.floor((clat - lat0) / grid_step_deg)
            j = math.floor((clon - lon0) / grid_step_deg)
            for di in (0, 1):
                for dj in (0, 1):
                    needed.add((
                        round(lat0 + (i + di) * grid_step_deg, 3),
                        round(lon0 + (j + dj) * grid_step_deg, 3),
                    ))
        return sorted(needed)

    @staticmethod
    def _parse_float_header(headers: Dict[str, Any], key: str) -> float | None:
        raw = headers.get(key)
        if raw is None:
            return None
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        return val if math.isfinite(val) else None

    @classmethod
    def _compute_rate_limit_wait(cls, headers: Dict[str, Any], attempt: int) -> float:
        retry_after = cls._parse_float_header(headers, "Retry-After")
        if retry_after is not None and retry_after > 0:
            return max(5.0, min(75.0, retry_after + 2.0))
        reset_ts = cls._parse_float_header(headers, "X-RateLimit-Reset")
        if reset_ts is not None:
            delta = reset_ts - time.time() + 2.0
            if 2.0 <= delta <= 75.0:
                return delta
        fallback = (15.0, 35.0, 62.0, 65.0, 65.0, 65.0)
        return fallback[min(attempt, len(fallback) - 1)]

    def fetch_weather_for_bbox_grid(
        self,
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
        grid_step_deg: float = WEATHER_GRID_STEP_DEG,
        past_days: int = 14,
        forecast_days: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        Genera una cuadrícula meteorológica regional regular y descarga sus métricas
        con reintentos automáticos y respeto estricto de cuotas Open-Meteo.
        """
        grid_coords: List[Tuple[float, float]] = []
        lat = min_lat
        while lat <= max_lat:
            lon = min_lon
            while lon <= max_lon:
                grid_coords.append((round(lat, 3), round(lon, 3)))
                lon += grid_step_deg
            lat += grid_step_deg

        return self.fetch_weather_for_coords(
            grid_coords,
            grid_step_deg=grid_step_deg,
            past_days=past_days,
            forecast_days=forecast_days,
        )

    def fetch_weather_for_coords(
        self,
        grid_coords: List[Tuple[float, float]],
        grid_step_deg: float = WEATHER_GRID_STEP_DEG,
        past_days: int = 28,
        forecast_days: int = 3,
    ) -> List[Dict[str, Any]]:
        """Descarga series meteorológicas para nodos dados respetando cuotas Open-Meteo."""
        if not grid_coords:
            return []

        print(
            f"[Meteo Grid] Descargando {len(grid_coords)} nodos ({grid_step_deg}°, "
            f"{past_days}d históricos + {forecast_days}d previsión)..."
        )
        results_by_location = {}
        batch_size = 25
        last_cost: float | None = None
        last_remaining: float | None = None
        last_reset: float | None = None

        for i in range(0, len(grid_coords), batch_size):
            chunk = grid_coords[i:i + batch_size]

            if last_remaining is not None:
                est_cost = max(
                    (last_cost or 0.0) * 1.15,
                    len(chunk) * ((past_days + forecast_days) / 14.0) + 25.0,
                )
                if last_remaining < est_cost:
                    wait_s = 62.0
                    if last_reset is not None:
                        delta = last_reset - time.time() + 2.0
                        if 2.0 <= delta <= 75.0:
                            wait_s = delta
                    wait_s = max(2.0, min(70.0, wait_s))
                    print(
                        f"   [Pacing Open-Meteo] Cuota restante baja "
                        f"({last_remaining:.1f} < {est_cost:.1f}); esperando {wait_s:.1f}s..."
                    )
                    time.sleep(wait_s)

            lats = ",".join(str(c[0]) for c in chunk)
            lngs = ",".join(str(c[1]) for c in chunk)

            params = {
                "latitude": lats,
                "longitude": lngs,
                "daily": [
                    "precipitation_sum",
                    "temperature_2m_mean",
                    "temperature_2m_min",
                    "soil_moisture_0_to_7cm_mean",
                    "soil_moisture_7_to_28cm_mean",
                    "soil_temperature_0_to_7cm_mean",
                    "soil_temperature_7_to_28cm_mean",
                    "et0_fao_evapotranspiration",
                    "wind_speed_10m_max",
                    "relative_humidity_2m_mean",
                    "snowfall_sum",
                    "shortwave_radiation_sum",
                ],
                "past_days": past_days,
                "forecast_days": forecast_days,
                "timezone": "Europe/Madrid",
            }

            data = None
            success = False
            for attempt in range(6):
                try:
                    resp = self.session.get(self.BASE_URL, params=params, timeout=30)
                    if resp.status_code == 429:
                        wait_t = self._compute_rate_limit_wait(resp.headers, attempt)
                        snippet = (resp.text or "").strip().replace("\n", " ")[:160]
                        print(
                            f"   [!] Open-Meteo 429 ({snippet}); "
                            f"esperando {wait_t:.1f}s (intento {attempt+1}/6)..."
                        )
                        time.sleep(wait_t)
                        continue
                    if resp.status_code in (500, 502, 503, 504):
                        wait_t = (4.0, 8.0, 15.0, 25.0, 40.0, 45.0)[min(attempt, 5)]
                        print(
                            f"   [!] Open-Meteo {resp.status_code}, "
                            f"reintentando en {wait_t:.0f}s (intento {attempt+1}/6)..."
                        )
                        time.sleep(wait_t)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    last_cost = self._parse_float_header(resp.headers, "X-RateLimit-Cost")
                    last_remaining = self._parse_float_header(resp.headers, "X-RateLimit-Remaining")
                    last_reset = self._parse_float_header(resp.headers, "X-RateLimit-Reset")
                    success = True
                    break
                except requests.RequestException as exc:
                    if attempt == 5:
                        raise
                    wait_t = (3.0, 6.0, 12.0, 20.0, 30.0)[min(attempt, 4)]
                    print(
                        f"   [!] Error de red Open-Meteo ({exc}); "
                        f"reintentando en {wait_t:.0f}s (intento {attempt+1}/6)..."
                    )
                    time.sleep(wait_t)

            if not success:
                raise RuntimeError("Open-Meteo no devolvió datos meteorológicos.")

            locations = data if isinstance(data, list) else [data]
            today = datetime.now(ZoneInfo("Europe/Madrid")).date()
            for loc in locations:
                daily = loc.get("daily", {})
                dates = daily.get("time", [])
                precipitation = daily.get("precipitation_sum", [])
                temperatures = daily.get("temperature_2m_mean", [])
                minimum_temperatures = daily.get("temperature_2m_min", [])
                soil_moisture = daily.get("soil_moisture_0_to_7cm_mean", [])
                soil_moisture_deep = daily.get("soil_moisture_7_to_28cm_mean", [])
                soil_temperatures = daily.get("soil_temperature_0_to_7cm_mean", [])
                soil_temperatures_deep = daily.get("soil_temperature_7_to_28cm_mean", [])
                evapotranspiration = daily.get("et0_fao_evapotranspiration", [])
                wind_speeds = daily.get("wind_speed_10m_max", [])
                rel_humidity = daily.get("relative_humidity_2m_mean", [])
                snowfalls = daily.get("snowfall_sum", [])
                radiations = daily.get("shortwave_radiation_sum", [])

                parsed_dates = [datetime.strptime(day, "%Y-%m-%d").date() for day in dates]
                candidate_days = [
                    idx for idx, parsed_d in enumerate(parsed_dates)
                    if today - timedelta(days=past_days) <= parsed_d <= today
                    and idx < len(precipitation) and precipitation[idx] is not None
                    and idx < len(temperatures) and temperatures[idx] is not None
                    and idx < len(minimum_temperatures) and minimum_temperatures[idx] is not None
                    and idx < len(soil_moisture) and soil_moisture[idx] is not None
                ]
                completed_days = candidate_days[-past_days:]
                if len(completed_days) != past_days:
                    raise RuntimeError(
                        f"Open-Meteo devolvió {len(completed_days)} días completos; "
                        f"se esperaban {past_days}."
                    )

                future_days = [
                    idx for idx, parsed_d in enumerate(parsed_dates)
                    if today < parsed_d <= today + timedelta(days=forecast_days)
                ][:forecast_days]

                def values_for(indices, values):
                    return [
                        values[idx] for idx in indices
                        if idx < len(values) and values[idx] is not None
                    ]

                precip = values_for(completed_days, precipitation)
                temp_mean = values_for(completed_days, temperatures)
                temp_min = values_for(completed_days, minimum_temperatures)
                soil_m = values_for(completed_days, soil_moisture)
                soil_m_deep = values_for(completed_days, soil_moisture_deep)
                soil_t = values_for(completed_days, soil_temperatures)
                soil_t_deep = values_for(completed_days, soil_temperatures_deep)
                et0_vals = values_for(completed_days, evapotranspiration)
                wind_vals = values_for(completed_days, wind_speeds)
                rh_vals = values_for(completed_days, rel_humidity)
                snow_vals = values_for(completed_days, snowfalls)
                rad_vals = values_for(completed_days, radiations)

                if any(len(values) != past_days for values in (
                    precip, temp_mean, temp_min, soil_m
                )):
                    raise RuntimeError(
                        "Open-Meteo devolvió datos diarios incompletos para "
                        f"({loc.get('latitude')}, {loc.get('longitude')})."
                    )

                # Combinar humedad superficial (0-7cm) y radicular (7-28cm)
                if len(soil_m_deep) == past_days:
                    combined_soil_m = [
                        s0 * 0.60 + s1 * 0.40
                        for s0, s1 in zip(soil_m, soil_m_deep)
                    ]
                else:
                    combined_soil_m = soil_m
                soil_m_recent_3d = sum(combined_soil_m[-3:]) / 3.0
                soil_m_mean_14d = sum(combined_soil_m[-14:]) / min(14, len(combined_soil_m))
                effective_soil_moisture_pct = round(
                    (soil_m_recent_3d * 0.65 + soil_m_mean_14d * 0.35) * 100.0, 2
                )

                # Temperatura del suelo superficial y radicular (0-7 cm + 7-28 cm)
                if len(soil_t) == past_days and len(soil_t_deep) == past_days:
                    combined_soil_t = [
                        t0 * 0.60 + t1 * 0.40
                        for t0, t1 in zip(soil_t, soil_t_deep)
                    ]
                else:
                    combined_soil_t = soil_t

                soil_temp_0_7cm_3d_celsius = (
                    round(sum(soil_t[-3:]) / 3.0, 2)
                    if len(soil_t) == past_days else None
                )
                soil_temp_root_mean_14d_celsius = (
                    round(
                        (sum(combined_soil_t[-3:]) / 3.0) * 0.65
                        + (sum(combined_soil_t[-14:]) / min(14, len(combined_soil_t))) * 0.35,
                        2,
                    )
                    if len(combined_soil_t) == past_days else None
                )

                precip_14 = precip[-14:]
                temp_mean_14 = temp_mean[-14:]
                temp_min_14 = temp_min[-14:]
                et0_14 = et0_vals[-14:] if len(et0_vals) == past_days else []

                et0_accum_14d = (
                    round(sum(et0_14), 2)
                    if len(et0_14) >= 14 else None
                )
                water_balance_14d = (
                    round(sum(precip_14) - sum(et0_14), 2)
                    if len(et0_14) >= 14 else None
                )
                # Choque térmico: diferencia entre últimos 4 días y primeros 6 de la ventana de 14 días
                thermal_shock = (
                    round(sum(temp_mean_14[-4:]) / 4.0 - sum(temp_mean_14[:6]) / 6.0, 2)
                    if len(temp_mean_14) >= 10 else None
                )
                wind_max_14d = (
                    round(max(wind_vals[-7:]), 2)
                    if len(wind_vals) == past_days else None
                )
                humidity_mean_7d = (
                    round(
                        (sum(rh_vals[-3:]) / 3.0) * 0.60
                        + (sum(rh_vals[-7:]) / 7.0) * 0.40,
                        2,
                    )
                    if len(rh_vals) >= 7 else None
                )

                fc_precip_vals = values_for(future_days, precipitation)
                fc_tmin_vals = values_for(future_days, minimum_temperatures)
                fc_tmean_vals = values_for(future_days, temperatures)
                fc_snow_vals = values_for(future_days, snowfalls)

                result = {
                    "latitude": loc.get("latitude"),
                    "longitude": loc.get("longitude"),
                    "elevation": loc.get("elevation"),
                    "precip_daily_mm": [round(value, 2) for value in precip_14],
                    "precip_daily_28d_mm": (
                        [round(value, 2) for value in precip]
                        if past_days >= 28 else None
                    ),
                    "soil_temp_daily_28d_c": (
                        [round(value, 2) for value in combined_soil_t]
                        if past_days >= 28 and len(combined_soil_t) == past_days else None
                    ),
                    "temp_min_daily_28d_c": (
                        [round(value, 2) for value in temp_min]
                        if past_days >= 28 else None
                    ),
                    "soil_moisture_daily_28d_pct": (
                        [round(value * 100.0, 2) for value in combined_soil_m]
                        if past_days >= 28 else None
                    ),
                    "precip_last_3d_mm": round(sum(precip_14[-3:]), 2),
                    "precip_accum_7d_mm": round(sum(precip_14[-7:]), 2),
                    "precip_accum_10d_mm": round(sum(precip_14[-10:]), 2),
                    "precip_accum_14d_mm": round(sum(precip_14), 2),
                    "precip_accum_28d_mm": round(sum(precip), 2) if past_days >= 28 else None,
                    "precip_15_28d_mm": round(sum(precip[:14]), 2) if past_days >= 28 else None,
                    "temp_mean_14d_celsius": round(sum(temp_mean_14) / len(temp_mean_14), 2),
                    "temp_mean_recent_3d_celsius": round(sum(temp_mean_14[-3:]) / 3.0, 2),
                    "temp_min_14d_celsius": round(min(temp_min_14), 2),
                    "temp_min_recent_3d_celsius": round(min(temp_min_14[-3:]), 2),
                    "soil_moisture_percent": effective_soil_moisture_pct,
                    "soil_temp_mean_14d_celsius": soil_temp_root_mean_14d_celsius,
                    "soil_temp_0_7cm_3d_celsius": soil_temp_0_7cm_3d_celsius,
                    "soil_temp_root_mean_14d_celsius": soil_temp_root_mean_14d_celsius,
                    "et0_accum_14d_mm": et0_accum_14d,
                    "water_balance_14d_mm": water_balance_14d,
                    "thermal_shock_celsius": thermal_shock,
                    "wind_max_14d_kmh": wind_max_14d,
                    "humidity_mean_7d_percent": humidity_mean_7d,
                    "snowfall_7d_cm": (
                        round(sum(snow_vals[-7:]), 2) if len(snow_vals) >= 7 else None
                    ),
                    "snowfall_3d_fc_cm": (
                        round(sum(fc_snow_vals), 2) if fc_snow_vals else None
                    ),
                    "radiation_mean_7d_mj": (
                        round(sum(rad_vals[-7:]) / 7.0, 2) if len(rad_vals) >= 7 else None
                    ),
                    "forecast_3d_precip_daily": (
                        [round(float(v), 1) for v in fc_precip_vals] if fc_precip_vals else None
                    ),
                    "forecast_3d_temp_min_daily": (
                        [round(float(v), 1) for v in fc_tmin_vals] if fc_tmin_vals else None
                    ),
                    "forecast_3d_precip_mm": (
                        round(sum(fc_precip_vals), 2) if fc_precip_vals else None
                    ),
                    "forecast_3d_temp_min_celsius": (
                        round(min(fc_tmin_vals), 2) if fc_tmin_vals else None
                    ),
                    "forecast_3d_temp_mean_celsius": (
                        round(sum(fc_tmean_vals) / len(fc_tmean_vals), 2)
                        if fc_tmean_vals else None
                    ),
                }
                location_key = (
                    round(result["latitude"], 4),
                    round(result["longitude"], 4),
                )
                results_by_location[location_key] = result

            if i + batch_size < len(grid_coords):
                time.sleep(self.request_delay)

        return list(results_by_location.values())

    @staticmethod
    def batch_interpolate_weather(
        target_coords: List[Tuple[float, float]],
        meteo_grid: List[Dict[str, Any]],
        grid_step_deg: float = WEATHER_GRID_STEP_DEG,
        target_elevations: List[ float | None ] | None = None,
    ) -> List[Dict[str, Any]]:
        """Interpola campos continuos con los cuatro nodos más cercanos.

        La interpolación suaviza los saltos de la asignación al nodo más cercano,
        y aplica corrección por gradiente térmico altitudinal cuando se conoce la cota.
        """
        if not target_coords:
            return []
        if not meteo_grid:
            raise ValueError("No hay estaciones meteorológicas para interpolar.")
        if any(
            not isinstance(node.get("latitude"), (int, float))
            or not isinstance(node.get("longitude"), (int, float))
            for node in meteo_grid
        ):
            raise ValueError("Todos los nodos meteorológicos deben incluir coordenadas.")

        import numpy as np
        from scipy.spatial import cKDTree

        mean_latitude = sum(lat for lat, _ in target_coords) / len(target_coords)
        longitude_scale = math.cos(math.radians(mean_latitude))
        grid_pts = np.array([
            [m["latitude"], m["longitude"] * longitude_scale] for m in meteo_grid
        ])
        tree = cKDTree(grid_pts)
        targets = np.array([
            [lat, lon * longitude_scale] for lat, lon in target_coords
        ])
        distances, indices = tree.query(targets, k=min(4, len(meteo_grid)))
        if len(meteo_grid) == 1:
            distances = distances.reshape(-1, 1)
            indices = indices.reshape(-1, 1)

        weather_fields = (
            "precip_accum_7d_mm",
            "precip_accum_10d_mm",
            "precip_accum_14d_mm",
            "temp_mean_14d_celsius",
            "temp_min_14d_celsius",
            "soil_moisture_percent",
        )
        optional_weather_fields = (
            "soil_temp_mean_14d_celsius",
            "soil_temp_0_7cm_3d_celsius",
            "soil_temp_root_mean_14d_celsius",
            "et0_accum_14d_mm",
            "water_balance_14d_mm",
            "thermal_shock_celsius",
            "wind_max_14d_kmh",
            "precip_last_3d_mm",
            "precip_accum_28d_mm",
            "precip_15_28d_mm",
            "temp_mean_recent_3d_celsius",
            "temp_min_recent_3d_celsius",
            "humidity_mean_7d_percent",
            "snowfall_7d_cm",
            "snowfall_3d_fc_cm",
            "radiation_mean_7d_mj",
            "forecast_3d_precip_mm",
            "forecast_3d_temp_min_celsius",
            "forecast_3d_temp_mean_celsius",
            "elevation",
        )
        interpolated = []
        for idx_cell, (target, node_distances, node_indices) in enumerate(zip(
            targets, distances, indices
        )):
            exact_matches = np.flatnonzero(node_distances <= 1e-12)
            if len(exact_matches):
                selected = [int(node_indices[exact_matches[0]])]
                weights = np.array([1.0])
            else:
                selected = [int(index) for index in node_indices]
                weights = 1.0 / np.maximum(node_distances, 1e-12) ** 2
                weights /= weights.sum()

            values = {}
            for field in weather_fields:
                samples = [
                    (meteo_grid[index].get(field), weights[position])
                    for position, index in enumerate(selected)
                ]
                available = [
                    (value, weight)
                    for value, weight in samples
                    if isinstance(value, (int, float)) and math.isfinite(value)
                ]
                if not available:
                    raise ValueError(
                        f"No hay valores numéricos para interpolar '{field}'."
                    )
                total_weight = sum(weight for _, weight in available)
                values[field] = round(
                    sum(value * weight for value, weight in available) / total_weight,
                    2,
                )

            for field in optional_weather_fields:
                samples = [
                    (meteo_grid[index].get(field), weights[position])
                    for position, index in enumerate(selected)
                ]
                available = [
                    (value, weight)
                    for value, weight in samples
                    if isinstance(value, (int, float)) and math.isfinite(value)
                ]
                if available:
                    total_weight = sum(weight for _, weight in available)
                    values[field] = round(
                        sum(value * weight for value, weight in available) / total_weight,
                        2,
                    )
                else:
                    values[field] = None

            # Ajuste térmico microclimático por diferencia de altitud entre la celda y la malla
            cell_elev = (
                target_elevations[idx_cell]
                if target_elevations is not None and idx_cell < len(target_elevations)
                else None
            )
            model_elev = values.pop("elevation", None)
            temp_lapse_adj = 0.0
            soil_lapse_adj = 0.0
            if (
                isinstance(cell_elev, (int, float))
                and math.isfinite(cell_elev)
                and isinstance(model_elev, (int, float))
                and math.isfinite(model_elev)
            ):
                elev_diff = max(-600.0, min(600.0, float(cell_elev) - float(model_elev)))
                temp_lapse_adj = -0.006 * elev_diff
                soil_lapse_adj = -0.0045 * elev_diff
                for t_field in (
                    "temp_mean_14d_celsius",
                    "temp_min_14d_celsius",
                    "temp_mean_recent_3d_celsius",
                    "temp_min_recent_3d_celsius",
                    "forecast_3d_temp_min_celsius",
                    "forecast_3d_temp_mean_celsius",
                ):
                    if values.get(t_field) is not None:
                        values[t_field] = round(values[t_field] + temp_lapse_adj, 2)
                for st_field in (
                    "soil_temp_mean_14d_celsius",
                    "soil_temp_0_7cm_3d_celsius",
                    "soil_temp_root_mean_14d_celsius",
                ):
                    if values.get(st_field) is not None:
                        values[st_field] = round(values[st_field] + soil_lapse_adj, 2)

            daily_series = [
                node.get("precip_daily_mm") for node in (
                    meteo_grid[index] for index in selected
                )
            ]
            if any(
                not isinstance(series, list)
                or len(series) != 14
                or any(
                    not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                    for value in series
                )
                for series in daily_series
            ):
                raise ValueError(
                    "La serie diaria de precipitación debe contener 14 valores "
                    "numéricos no negativos."
                )
            values["precip_daily_mm"] = [
                round(
                    sum(
                        series[day] * weights[position]
                        for position, series in enumerate(daily_series)
                    ) / weights.sum(),
                    2,
                )
                for day in range(14)
            ]
            if values.get("precip_last_3d_mm") is None:
                values["precip_last_3d_mm"] = round(sum(values["precip_daily_mm"][-3:]), 2)

            def interpolate_series(
                field_name: str,
                exact_len: int | None = None,
                offset: float = 0.0,
                non_negative: bool = False,
                digits: int = 2,
            ):
                series_list = [meteo_grid[index].get(field_name) for index in selected]
                if any(
                    not isinstance(s, list)
                    or not s
                    or (exact_len is not None and len(s) != exact_len)
                    or any(
                        not isinstance(v, (int, float))
                        or not math.isfinite(v)
                        or (non_negative and v < 0)
                        for v in s
                    )
                    for s in series_list
                ):
                    return None
                target_len = exact_len if exact_len is not None else min(len(s) for s in series_list)
                w_sum = float(weights.sum())
                return [
                    round(
                        float(
                            sum(s[day] * float(weights[pos]) for pos, s in enumerate(series_list)) / w_sum + offset
                        ),
                        digits,
                    )
                    for day in range(target_len)
                ]

            p28_series = interpolate_series("precip_daily_28d_mm", exact_len=28, non_negative=True)
            st28_series = interpolate_series("soil_temp_daily_28d_c", exact_len=28, offset=soil_lapse_adj)
            tmin28_series = interpolate_series("temp_min_daily_28d_c", exact_len=28, offset=temp_lapse_adj)
            sm28_series = interpolate_series("soil_moisture_daily_28d_pct", exact_len=28, non_negative=True)
            fcr3d_series = interpolate_series("forecast_3d_precip_daily", non_negative=True, digits=1)
            fctn3d_series = interpolate_series("forecast_3d_temp_min_daily", offset=temp_lapse_adj, digits=1)

            values["precip_daily_28d_mm"] = p28_series
            values["soil_temp_daily_28d_c"] = st28_series
            values["temp_min_daily_28d_c"] = tmin28_series
            values["soil_moisture_daily_28d_pct"] = sm28_series
            values["forecast_3d_precip_daily"] = fcr3d_series
            values["forecast_3d_temp_min_daily"] = fctn3d_series

            if tmin28_series is not None:
                values["frost_days_7d"] = int(sum(1 for t in tmin28_series[-7:] if t < 0.5))
                values["frost_days_14d"] = int(sum(1 for t in tmin28_series[-14:] if t < 0.5))
                values["hard_frost_28d"] = bool(min(tmin28_series) <= -1.0)
            else:
                values["frost_days_7d"] = None
                values["frost_days_14d"] = None
                values["hard_frost_28d"] = bool(values["temp_min_14d_celsius"] <= -1.0)

            if p28_series is not None:
                max_dry_streak = 0
                cur_streak = 0
                for rain_day in p28_series[:21]:
                    if rain_day < 1.0:
                        cur_streak += 1
                        if cur_streak > max_dry_streak:
                            max_dry_streak = cur_streak
                    else:
                        cur_streak = 0
                values["pre_rain_dry_streak_days"] = max_dry_streak
            else:
                values["pre_rain_dry_streak_days"] = None

            if sm28_series is not None:
                values["pre_rain_min_soil_moisture_pct"] = round(float(min(sm28_series[:21])), 2)
            else:
                values["pre_rain_min_soil_moisture_pct"] = None

            nearest_node = meteo_grid[int(node_indices[0])]
            nearest_distance_km = math.hypot(
                target[0] - nearest_node["latitude"],
                target[1] - nearest_node["longitude"] * longitude_scale,
            ) * 111.32
            interpolated.append({
                **values,
                "weather_source": WEATHER_SOURCE,
                "weather_sampling_step_deg": grid_step_deg,
                "weather_interpolation": WEATHER_INTERPOLATION_METHOD,
                "weather_nearest_distance_km": round(nearest_distance_km, 2),
            })
        return interpolated

    @staticmethod
    def interpolate_nearest_weather(lat: float, lon: float, meteo_grid: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Encuentra el nodo meteorológico más cercano usando distancia euclidiana."""
        if not meteo_grid:
            raise ValueError("No hay estaciones meteorológicas para interpolar.")

        best_node = None
        min_dist_sq = float("inf")

        for node in meteo_grid:
            d_lat = lat - node["latitude"]
            d_lon = (lon - node["longitude"]) * math.cos(math.radians(lat))
            dist_sq = d_lat * d_lat + d_lon * d_lon
            if dist_sq < min_dist_sq:
                min_dist_sq = dist_sq
                best_node = node

        return best_node
