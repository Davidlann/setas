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

    def __init__(self, request_delay_seconds: float = 1.0):
        self.session = requests.Session()
        self.request_delay = request_delay_seconds

    def fetch_weather_for_bbox_grid(
        self,
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
        grid_step_deg: float = WEATHER_GRID_STEP_DEG,
        past_days: int = 14
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

        print(f"[Meteo Grid] Descargando {len(grid_coords)} estaciones de la malla regional ({grid_step_deg}°)...")
        results_by_location = {}
        batch_size = 20 # Lotes pequeños para no saturar burst limit

        for i in range(0, len(grid_coords), batch_size):
            chunk = grid_coords[i:i + batch_size]
            lats = ",".join(str(c[0]) for c in chunk)
            lngs = ",".join(str(c[1]) for c in chunk)

            params = {
                "latitude": lats,
                "longitude": lngs,
                "daily": [
                    "precipitation_sum",
                    "temperature_2m_mean",
                    "temperature_2m_min",
                    "soil_moisture_0_to_7cm_mean"
                ],
                "past_days": past_days,
                "forecast_days": 1,
                "timezone": "Europe/Madrid"
            }

            # Manejo de reintentos en caso de 429 o errores de red.
            success = False
            for attempt in range(4):
                try:
                    resp = self.session.get(self.BASE_URL, params=params, timeout=20)
                    if resp.status_code == 429:
                        wait_t = 3 * (attempt + 1)
                        print(f"   [!] Rate limit (429), pausando {wait_t}s...")
                        time.sleep(wait_t)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    success = True
                    break
                except requests.RequestException:
                    if attempt == 3:
                        raise
                    time.sleep(2)

            if not success:
                raise RuntimeError("Open-Meteo no devolvió datos meteorológicos.")

            locations = data if isinstance(data, list) else [data]
            for loc in locations:
                daily = loc.get("daily", {})
                dates = daily.get("time", [])
                precipitation = daily.get("precipitation_sum", [])
                temperatures = daily.get("temperature_2m_mean", [])
                minimum_temperatures = daily.get("temperature_2m_min", [])
                soil_moisture = daily.get("soil_moisture_0_to_7cm_mean", [])

                today = datetime.now(ZoneInfo("Europe/Madrid")).date()
                completed_days = [
                    i for i, day in enumerate(dates)
                    if today - timedelta(days=past_days)
                    <= datetime.strptime(day, "%Y-%m-%d").date() < today
                ]
                if len(completed_days) != past_days:
                    raise RuntimeError(
                        f"Open-Meteo devolvió {len(completed_days)} días completos; "
                        f"se esperaban {past_days}."
                    )

                def values_for(indices, values):
                    return [
                        values[i] for i in indices
                        if i < len(values) and values[i] is not None
                    ]

                precip = values_for(completed_days, precipitation)
                temp_mean = values_for(completed_days, temperatures)
                temp_min = values_for(completed_days, minimum_temperatures)
                soil_m = values_for(completed_days, soil_moisture)
                if any(len(values) != past_days for values in (
                    precip, temp_mean, temp_min, soil_m
                )):
                    raise RuntimeError(
                        "Open-Meteo devolvió datos diarios incompletos para "
                        f"({loc.get('latitude')}, {loc.get('longitude')})."
                    )

                result = {
                    "latitude": loc.get("latitude"),
                    "longitude": loc.get("longitude"),
                    "elevation": loc.get("elevation"),
                    "precip_daily_mm": [round(value, 2) for value in precip],
                    "precip_accum_7d_mm": round(sum(precip[-7:]), 2),
                    "precip_accum_10d_mm": round(sum(precip[-10:]), 2),
                    "precip_accum_14d_mm": round(sum(precip), 2),
                    "temp_mean_14d_celsius": round(sum(temp_mean) / len(temp_mean), 2),
                    "temp_min_14d_celsius": round(min(temp_min), 2),
                    "soil_moisture_percent": round((sum(soil_m) / len(soil_m)) * 100.0, 2)
                }
                location_key = (
                    round(result["latitude"], 4),
                    round(result["longitude"], 4),
                )
                results_by_location[location_key] = result

            time.sleep(self.request_delay)

        return list(results_by_location.values())

    @staticmethod
    def batch_interpolate_weather(
        target_coords: List[Tuple[float, float]],
        meteo_grid: List[Dict[str, Any]],
        grid_step_deg: float = WEATHER_GRID_STEP_DEG,
    ) -> List[Dict[str, Any]]:
        """Interpola campos continuos con los cuatro nodos más cercanos.

        La interpolación suaviza los saltos de la asignación al nodo más cercano,
        pero no aumenta la resolución ni la precisión del modelo meteorológico.
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
        interpolated = []
        for target, node_distances, node_indices in zip(
            targets, distances, indices
        ):
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
