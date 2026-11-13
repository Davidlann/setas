"""Motor experimental de puntuación micológica basado en reglas heurísticas."""
from datetime import date
from typing import Dict, Any, Optional
import yaml
from pathlib import Path
import math
import unicodedata

class MushroomScoringEngine:
    # Pesos de retardo post-lluvia ordenados desde hoy (índice 0) hacia atrás (hace 13 días)
    PRECIPITATION_LAG_PROFILES = {
        # Especies otoñales/frías: incubación pausada, pico óptimo entre 6 y 11 días atrás
        "autumn_cool": (
            0.05, 0.10, 0.20, 0.35, 0.60, 0.80, 0.95,
            1.00, 1.00, 1.00, 0.95, 0.75, 0.50, 0.30,
        ),
        # Especies termófilas/estivales: brotada más rápida, pico entre 4 y 9 días atrás
        "warm_fast": (
            0.10, 0.20, 0.35, 0.55, 0.85, 0.95, 1.00,
            1.00, 0.95, 0.85, 0.70, 0.50, 0.30, 0.15,
        ),
    }
    PRECIPITATION_LAG_WEIGHTS = PRECIPITATION_LAG_PROFILES["autumn_cool"]

    def __init__(self, matrix_path: Optional[str] = None):
        if matrix_path is None:
            config_dir = Path(__file__).resolve().parent.parent.parent / "config"
            matrix_path = config_dir / "species_matrix.yaml"

        with open(matrix_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
            self.species_matrix = self.config.get("species", {})

    @staticmethod
    def resolve_season_phase(snapshot_date: Optional[str | date] = None) -> str:
        """Determina automáticamente la fase estacional ('early' o 'late') según la fecha."""
        if snapshot_date is None:
            return "early"
        if isinstance(snapshot_date, str):
            try:
                parsed = date.fromisoformat(snapshot_date[:10])
            except ValueError:
                return "early"
        elif isinstance(snapshot_date, date):
            parsed = snapshot_date
        else:
            return "early"
        # Desde mediados de octubre hasta principios de abril predomina la búsqueda en solanas ('late')
        if (parsed.month == 10 and parsed.day >= 15) or parsed.month in (11, 12, 1, 2, 3):
            return "late"
        return "early"

    @staticmethod
    def _effective_soil_ph(cell_static_data: Dict[str, Any]) -> float:
        """Calcula el pH efectivo del horizonte húmico combinando media y cuantil ácido (Q05)."""
        soil_ph = float(cell_static_data["soil_ph"])
        ph_q05 = cell_static_data.get("soil_ph_q05")
        if (
            isinstance(ph_q05, (int, float))
            and math.isfinite(ph_q05)
            and 0.0 <= ph_q05 <= soil_ph
        ):
            return round(soil_ph * 0.60 + float(ph_q05) * 0.40, 2)
        return soil_ph

    @staticmethod
    def _compute_phenology_factor(
        meteo_rules: Dict[str, Any],
        snapshot_date: Optional[str],
        elevation_m: Optional[float] = None,
    ) -> tuple[float, Optional[str]]:
        """Calcula el factor fenológico estacional (0.05–1.0) según fecha y altitud."""
        weights = meteo_rules.get("phenology_monthly_weights")
        if not weights or not snapshot_date:
            return 1.0, None
        try:
            parsed = date.fromisoformat(str(snapshot_date)[:10])
        except ValueError:
            return 1.0, None

        month = parsed.month
        next_month = 1 if month == 12 else month + 1
        prev_month = 12 if month == 1 else month - 1
        w_curr = float(weights.get(month, weights.get(str(month), 1.0)))
        if parsed.day >= 15:
            w_adj = float(weights.get(next_month, weights.get(str(next_month), w_curr)))
            progress = (parsed.day - 15) / 30.0
            base_factor = w_curr * (1.0 - progress) + w_adj * progress
        else:
            w_adj = float(weights.get(prev_month, weights.get(str(prev_month), w_curr)))
            progress = (15 - parsed.day) / 30.0
            base_factor = w_curr * (1.0 - progress) + w_adj * progress

        # Ajuste bioclimático por altitud en otoño tardío / primavera
        if isinstance(elevation_m, (int, float)) and math.isfinite(elevation_m):
            if month in (11, 12) and elevation_m >= 1300:
                # En montaña (>=1300 m) en noviembre y diciembre la parada invernal llega antes
                alt_penalty = min(0.45, (elevation_m - 1300.0) / 1400.0)
                base_factor = max(0.15, base_factor - alt_penalty)
            elif month in (8, 9) and elevation_m >= 1400:
                # En alta montaña la campaña otoñal arranca antes (finales de agosto/septiembre)
                base_factor = min(1.0, base_factor + 0.12)

        factor = round(max(0.05, min(1.0, base_factor)), 2)
        status = (
            "peak_season" if factor >= 0.80
            else "active_season" if factor >= 0.50
            else "out_of_season"
        )
        return factor, status

    def calculate_cell_score(
        self,
        species_id: str,
        cell_static_data: Dict[str, Any],
        cell_meteo_data: Dict[str, Any],
        season_phase: str = "early" # "early" o "late"
    ) -> Dict[str, Any]:
        if cell_static_data.get("environmental_data_quality") != "verified":
            raise ValueError(
                "No se calcula un índice validado con variables ambientales sin verificar."
            )
        return self._calculate_score(
            species_id, cell_static_data, cell_meteo_data, season_phase
        )

    def calculate_relative_suitability(
        self,
        species_id: str,
        cell_static_data: Dict[str, Any],
        cell_meteo_data: Dict[str, Any],
        season_phase: str = "early"
    ) -> Dict[str, Any]:
        """Calcula un índice orientativo; no es una probabilidad ni un score validado."""
        if cell_static_data.get("environmental_data_quality") != "sourced_unvalidated":
            raise ValueError(
                "El índice orientativo requiere variables con fuente identificada "
                "y estado sourced_unvalidated."
            )
        result = self._calculate_score(
            species_id,
            cell_static_data,
            cell_meteo_data,
            season_phase,
            allow_missing_aspect=True,
            relative_mode=True,
        )
        result["data_quality"] = "sourced_unvalidated"
        result["score_type"] = "experimental_separated_habitat_weather"
        result["combined_score"] = round(
            math.sqrt(result["score"] * result["weather_score"]), 1
        )
        return result

    def has_potential_habitat(self, cell_static_data: Dict[str, Any]) -> bool:
        """Return whether any target species passes the static habitat exclusions."""
        for species_id in self.species_matrix:
            _, exclusion_reason = self._static_habitat_exclusion(
                species_id, cell_static_data, allow_unknown_host=True
            )
            if exclusion_reason is None:
                return True
        return False

    def _calculate_score(
        self,
        species_id: str,
        cell_static_data: Dict[str, Any],
        cell_meteo_data: Dict[str, Any],
        season_phase: str,
        allow_missing_aspect: bool = False,
        relative_mode: bool = False,
    ) -> Dict[str, Any]:
        """
        Calcula el índice de fructificación (0 a 100) para una celda H3 específica.

        cell_static_data:
            - dominant_species: str (ej. 'Pinus sylvestris')
            - canopy_cover: float (0-100)
            - elevation_m: float
            - slope_deg: float
            - aspect_cardinal: str ('N', 'S', 'E', 'W', etc.)
            - soil_ph: float (ej. 5.2)

        cell_meteo_data:
            - precip_accum_14d_mm: float
            - temp_mean_14d_celsius: float
            - temp_min_14d_celsius: float
            - soil_moisture_percent: float
        """
        rules = self.species_matrix.get(species_id)
        if not rules:
            raise ValueError(f"Especie '{species_id}' no encontrada en la matriz ecológica.")

        required_static = [
            "dominant_species", "canopy_cover", "elevation_m",
            "soil_ph", "slope_deg"
        ]
        if not allow_missing_aspect:
            required_static.append("aspect_cardinal")
        required_meteo = (
            "precip_accum_14d_mm", "temp_mean_14d_celsius",
            "temp_min_14d_celsius", "soil_moisture_percent"
        )
        missing_static = [key for key in required_static if cell_static_data.get(key) is None]
        missing_meteo = [key for key in required_meteo if cell_meteo_data.get(key) is None]
        if missing_static or missing_meteo:
            missing = missing_static + missing_meteo
            raise ValueError(f"Faltan variables necesarias para el índice: {', '.join(missing)}")
        if (
            not isinstance(cell_static_data["dominant_species"], str)
            or not cell_static_data["dominant_species"].strip()
        ):
            raise ValueError("La formación forestal no contiene un nombre válido.")
        numeric_static = ("canopy_cover", "elevation_m", "soil_ph", "slope_deg")
        for key in numeric_static + required_meteo:
            value = (cell_static_data if key in required_static else cell_meteo_data)[key]
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"La variable '{key}' no contiene un valor numérico válido.")
        texture_fields = (
            "soil_sand_percent", "soil_silt_percent", "soil_clay_percent"
        )
        for key in texture_fields:
            value = cell_static_data.get(key)
            if value is not None and (
                not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 100
            ):
                raise ValueError(
                    f"La variable '{key}' debe estar entre 0 y 100 %."
                )
        texture_is_complete = all(
            cell_static_data.get(key) is not None for key in texture_fields
        )
        if texture_is_complete and sum(
            cell_static_data[key] for key in texture_fields
        ) <= 0:
            raise ValueError("La composición del suelo no puede sumar cero.")
        aspect = cell_static_data.get("aspect_cardinal")
        if aspect is None and not allow_missing_aspect:
            raise ValueError("La orientación es obligatoria para el índice validado.")
        aspect_es_to_en = {"SO": "SW", "O": "W", "NO": "NW"}
        if aspect is not None:
            if not isinstance(aspect, str):
                raise ValueError("La orientación debe ser una dirección cardinal válida.")
            aspect_norm = aspect_es_to_en.get(aspect.strip().upper(), aspect.strip().upper())
            if aspect_norm not in {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}:
                raise ValueError("La orientación debe ser una dirección cardinal válida.")
        else:
            aspect_norm = None
        if not 0 <= cell_static_data["canopy_cover"] <= 100:
            raise ValueError("La cobertura de copa debe estar entre 0 y 100.")
        if not 0 <= cell_static_data["soil_ph"] <= 14:
            raise ValueError("El pH del suelo debe estar entre 0 y 14.")
        if not 0 <= cell_static_data["slope_deg"] <= 90:
            raise ValueError("La pendiente debe estar entre 0 y 90 grados.")
        if cell_meteo_data["precip_accum_14d_mm"] < 0:
            raise ValueError("La precipitación acumulada no puede ser negativa.")
        daily_precipitation = cell_meteo_data.get("precip_daily_mm")
        if daily_precipitation is not None and (
            not isinstance(daily_precipitation, list)
            or len(daily_precipitation) != len(self.PRECIPITATION_LAG_WEIGHTS)
            or any(
                not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                for value in daily_precipitation
            )
        ):
            raise ValueError(
                "precip_daily_mm debe contener 14 valores diarios no negativos "
                "ordenados del más antiguo al más reciente."
            )
        if not 0 <= cell_meteo_data["soil_moisture_percent"] <= 100:
            raise ValueError("La humedad volumétrica debe estar entre 0 y 100 %.")
        if season_phase not in ("early", "late"):
            raise ValueError("season_phase debe ser 'early' o 'late'.")

        # ==============================================================
        # 1. MÁSCARA DE EXCLUSIÓN BIOLÓGICA (Condiciones sine qua non)
        # ==============================================================
        edaphic = rules["edaphic"]

        dominant_tree = cell_static_data["dominant_species"]
        canopy_cover = cell_static_data["canopy_cover"]
        elevation = cell_static_data["elevation_m"]
        soil_ph = cell_static_data["soil_ph"]
        effective_soil_ph = self._effective_soil_ph(cell_static_data)

        host_assessment, exclusion_reason = self._static_habitat_exclusion(
            species_id, cell_static_data, allow_unknown_host=relative_mode
        )
        host_details = {
            "host_assessment": (
                "compatible" if host_assessment is True
                else "unknown" if host_assessment is None
                else "incompatible"
            ),
            "host_tree": dominant_tree,
        }
        if exclusion_reason is not None and not relative_mode:
            return self._build_result(
                0.0, exclusion_reason, breakdown=host_details
            )

        # ==============================================================
        # 2. PONDERACIÓN METEOROLÓGICA (Máximo 45 puntos)
        # ==============================================================
        meteo_rules = rules["meteorology"]
        precip_14d = cell_meteo_data["precip_accum_14d_mm"]
        temp_mean = cell_meteo_data["temp_mean_14d_celsius"]
        temp_min = cell_meteo_data["temp_min_14d_celsius"]
        soil_moisture = cell_meteo_data["soil_moisture_percent"]

        # Factor Precipitación (0 a 25 pts)
        thresh_precip = meteo_rules["accumulated_precip_14d_threshold_mm"]
        opt_precip = meteo_rules["accumulated_precip_14d_optimal_mm"]
        lag_profile_name = meteo_rules.get("precipitation_lag_profile", "autumn_cool")
        lag_weights = self.PRECIPITATION_LAG_PROFILES.get(
            lag_profile_name, self.PRECIPITATION_LAG_WEIGHTS
        )
        effective_precip = precip_14d
        precip_incubation_mm = None
        precip_recent_4d_mm = cell_meteo_data.get("precip_last_3d_mm")
        rain_timing_status = None
        if daily_precipitation is not None:
            weight_mean = sum(lag_weights) / len(lag_weights)
            effective_precip = sum(
                rain * weight
                for rain, weight in zip(
                    reversed(daily_precipitation),
                    lag_weights,
                )
            ) / weight_mean
            # Los 10 primeros valores son hace 4–13 días (ventana de incubación);
            # los últimos 4 son hoy y los 3 días anteriores (lluvia demasiado reciente para brotar hoy).
            precip_incubation_mm = round(sum(daily_precipitation[:10]), 2)
            precip_recent_4d_mm = round(sum(daily_precipitation[10:]), 2)
            if precip_incubation_mm < thresh_precip * 0.25:
                # Freno por dormancia: si apenas llovió hace 4–13 días, la tormenta de ayer/hoy aún no ha incubado
                effective_precip = min(effective_precip, thresh_precip * 0.55)
                rain_timing_status = (
                    "too_recent_for_flush"
                    if precip_recent_4d_mm >= thresh_precip * 0.35
                    else "dry"
                )
            elif precip_incubation_mm < thresh_precip * 0.75:
                # Si la lluvia en ventana de incubación (hace 4–13 días) no alcanza el 75% del umbral,
                # limitamos cuánto puede inflar la lluvia de los últimos 3–4 días para que no alcance el óptimo prematuramente
                max_allowed = min(opt_precip * 0.85, precip_incubation_mm * 2.2)
                if effective_precip > max_allowed:
                    effective_precip = max_allowed
                rain_timing_status = (
                    "optimal_incubation"
                    if precip_incubation_mm >= thresh_precip * 0.60
                    else "moderate"
                )
            else:
                rain_timing_status = "optimal_incubation"
        else:
            if precip_14d < thresh_precip * 0.35:
                rain_timing_status = "dry"
            elif precip_14d >= thresh_precip:
                rain_timing_status = "optimal_incubation"
            else:
                rain_timing_status = "moderate"

        # Ajuste por balance hídrico (evapotranspiración) si está disponible
        water_balance = cell_meteo_data.get("water_balance_14d_mm")
        if water_balance is not None:
            if water_balance < 0:
                drying_factor = max(0.75, 1.0 + (water_balance / 140.0))
                effective_precip *= drying_factor
            elif water_balance > 25.0:
                effective_precip = min(opt_precip * 1.2, effective_precip * 1.05)

        if effective_precip < thresh_precip:
            # Lluvia insuficiente para romper la dormancia miceliar
            precip_pts = (effective_precip / thresh_precip) * 8.0
        elif effective_precip >= opt_precip:
            precip_pts = 25.0
        else:
            # Rango entre umbral y óptimo
            progress = (effective_precip - thresh_precip) / (opt_precip - thresh_precip)
            precip_pts = 8.0 + progress * 17.0

        # Factor Temperatura Térmica (0 a 15 pts)
        # Si se dispone de temperatura del suelo superficial (0-7 cm), se pondera con la temperatura ambiental
        soil_temp = cell_meteo_data.get("soil_temp_mean_14d_celsius")
        temp_recent_3d = cell_meteo_data.get("temp_mean_recent_3d_celsius")
        air_temp_ref = (
            temp_recent_3d * 0.6 + temp_mean * 0.4
            if isinstance(temp_recent_3d, (int, float)) and math.isfinite(temp_recent_3d)
            else temp_mean
        )
        if soil_temp is not None and "soil_temp_optimal_celsius" in meteo_rules:
            t_opt_min, t_opt_max = meteo_rules["soil_temp_optimal_celsius"]
            eval_temp = soil_temp * 0.7 + air_temp_ref * 0.3
        else:
            t_opt_min, t_opt_max = meteo_rules["mean_temp_optimal_celsius"]
            eval_temp = air_temp_ref

        if t_opt_min <= eval_temp <= t_opt_max:
            temp_pts = 15.0
        elif abs(eval_temp - t_opt_min) <= 4.0 or abs(eval_temp - t_opt_max) <= 4.0:
            temp_pts = 9.0
        else:
            temp_pts = 3.0

        # Choque térmico (disparador fisiológico de fructificación otoñal/estival)
        thermal_shock = cell_meteo_data.get("thermal_shock_celsius")
        thermal_shock_status = None
        thermal_shock_adj = 0.0
        if thermal_shock is not None:
            shock_trigger = meteo_rules.get("thermal_shock_trigger")
            if shock_trigger is True:
                # Especies otoñales que responden a caída térmica tras lluvia acumulada
                if thermal_shock <= -2.0 and effective_precip >= thresh_precip:
                    thermal_shock_adj = 2.5
                    thermal_shock_status = "triggered_favorable"
                elif thermal_shock > 2.0:
                    thermal_shock_adj = -2.0
                    thermal_shock_status = "warming_inhibited"
                else:
                    thermal_shock_status = "stable"
            elif shock_trigger is False:
                # Especies termófilas que se inhiben ante frío prematuro
                if thermal_shock <= -3.5:
                    thermal_shock_adj = -4.0
                    thermal_shock_status = "cold_suppressed"
                elif thermal_shock >= 0.0:
                    thermal_shock_status = "warm_favorable"
                else:
                    thermal_shock_status = "stable"

        # Penalización por heladas destructivas del carpóforo (priorizando últimos 3 días si existe,
        # pero manteniendo penalización parcial si hubo heladas fuertes durante la ventana de incubación de 14d)
        min_limit = meteo_rules.get("min_temp_limit_celsius", 0.0)
        temp_min_recent = cell_meteo_data.get("temp_min_recent_3d_celsius")
        has_recent_min = (
            isinstance(temp_min_recent, (int, float))
            and math.isfinite(temp_min_recent)
        )
        if has_recent_min:
            if float(temp_min_recent) < min_limit:
                temp_pts *= 0.3  # Helada reciente en los últimos 3 días: daño directo al carpóforo
            elif temp_min < min_limit - 3.0:
                temp_pts *= 0.65  # Helada severa durante la incubación (14d)
            elif temp_min < min_limit:
                temp_pts *= 0.85  # Helada moderada previa en la ventana de 14d
        else:
            if temp_min < min_limit:
                temp_pts *= 0.3  # Sin desglose de 3 días: penalización estándar por helada en 14d

        # Factor Humedad del suelo (0 a 5 pts)
        min_soil_m = meteo_rules.get("soil_moisture_min_percent", 20.0)
        soil_pts = 5.0 if soil_moisture >= min_soil_m else (soil_moisture / min_soil_m) * 5.0

        # Ajuste por humedad relativa ambiental reciente (si está disponible)
        humidity_7d = cell_meteo_data.get("humidity_mean_7d_percent")
        humidity_adj = 0.0
        if isinstance(humidity_7d, (int, float)) and math.isfinite(humidity_7d):
            if humidity_7d >= 72.0 and effective_precip >= thresh_precip * 0.6:
                humidity_adj = min(2.0, (humidity_7d - 72.0) / 10.0)
            elif humidity_7d < 45.0:
                humidity_adj = -min(2.5, (45.0 - humidity_7d) / 8.0)

        score_meteo = precip_pts + temp_pts + soil_pts + thermal_shock_adj + humidity_adj
        score_meteo = max(0.0, min(45.0, score_meteo))

        # Factor viento (desecación de primordios en cumbres/laderas)
        wind_max = cell_meteo_data.get("wind_max_14d_kmh")
        wind_penalty = 0.0
        if wind_max is not None:
            wind_limit = meteo_rules.get("wind_max_limit_kmh", 45.0)
            if wind_max > wind_limit:
                wind_penalty = min(0.20, (wind_max - wind_limit) / 100.0)
                score_meteo *= (1.0 - wind_penalty)

        # Factor fenológico estacional (según fecha del snapshot y altitud)
        snapshot_date = cell_meteo_data.get("snapshot_date")
        phenology_factor, season_status = self._compute_phenology_factor(
            meteo_rules, snapshot_date, elevation_m=elevation
        )
        score_meteo *= phenology_factor

        weather_score = min(100.0, round(score_meteo / 45.0 * 100.0, 1))

        # ==============================================================
        # 3. PONDERACIÓN EDAFOLÓGICA (Máximo 35 puntos)
        # ==============================================================
        ph_opt_min, ph_opt_max = edaphic["ph_optimal"]
        ph_tol_min, ph_tol_max = edaphic["ph_tolerated"]
        if ph_opt_min <= effective_soil_ph <= ph_opt_max:
            score_ph = 35.0
        else:
            # Distancia al rango óptimo dentro del tolerado
            if effective_soil_ph < ph_opt_min:
                dist = ph_opt_min - effective_soil_ph
                max_d = ph_opt_min - ph_tol_min
            else:
                dist = effective_soil_ph - ph_opt_max
                max_d = ph_tol_max - ph_opt_max

            ratio = max(0.0, 1.0 - (dist / max(0.1, max_d)))
            score_ph = 15.0 + ratio * 20.0

        texture_score = None
        if texture_is_complete:
            texture_score = self._soil_texture_score(
                cell_static_data["soil_sand_percent"],
                cell_static_data["soil_silt_percent"],
                cell_static_data["soil_clay_percent"],
            )
            score_edaphic = score_ph * (28.0 / 35.0) + texture_score
        else:
            score_edaphic = score_ph

        # ==============================================================
        # 4. PONDERACIÓN TOPOGRÁFICA (Máximo 20 puntos)
        # ==============================================================
        topo_rules = rules["topography"]
        slope = cell_static_data["slope_deg"]
        aspect = aspect_norm

        # Penalización por pendiente excesiva (lavado de esporas / erosión hídrica)
        max_slope = topo_rules.get("slope_max_deg", 45)
        slope_pts = 10.0 if slope <= (max_slope * 0.7) else max(0.0, (1.0 - (slope / max_slope)) * 10.0)

        # Orientación / Exposición según fase estacional
        pref_aspects = (
            topo_rules.get("aspect_preference_early_season", [])
            if season_phase == "early"
            else topo_rules.get("aspect_preference_late_season", [])
        )
        aspect_pts = 10.0 if aspect in pref_aspects else 5.0

        score_topo = slope_pts + aspect_pts

        # ==============================================================
        # TOTAL SCORE (0 a 100)
        # ==============================================================
        if relative_mode:
            habitat_score = score_edaphic + score_topo
            total_score = 100.0 * (habitat_score / 55.0)
            if exclusion_reason is not None:
                total_score = 0.0
        else:
            total_score = score_meteo + score_edaphic + score_topo
        if host_assessment is None:
            total_score *= 0.82
        total_score = min(100.0, round(total_score, 1))

        result = self._build_result(
            total_score,
            reason=exclusion_reason or (
                "Índice heurístico; la formación forestal no permite identificar "
                "con certeza el hospedador"
                if host_assessment is None
                else "Índice heurístico experimental; no validado con fructificaciones observadas"
            ),
            breakdown={
                "meteorology": round(score_meteo, 1),
                "soil_ph_score": round(score_ph, 1),
                "soil_texture_score": (
                    round(texture_score, 1)
                    if texture_score is not None else None
                ),
                "soil_texture_status": (
                    "scored" if texture_score is not None else "missing"
                ),
                "soil_texture_percent": (
                    {
                        "sand": cell_static_data["soil_sand_percent"],
                        "silt": cell_static_data["soil_silt_percent"],
                        "clay": cell_static_data["soil_clay_percent"],
                    }
                    if texture_score is not None else None
                ),
                "weather_score": weather_score,
                "edaphic": round(score_edaphic, 1),
                "topography": round(score_topo, 1),
                "combination_method": (
                    "separate_habitat_and_weather"
                    if relative_mode else "additive_experimental"
                ),
                "host_assessment": (
                    "compatible" if host_assessment is True
                    else "unknown" if host_assessment is None
                    else "incompatible"
                ),
                "precip_14d_mm": precip_14d,
                "effective_precip_14d_mm": round(effective_precip, 2),
                "precip_incubation_mm": precip_incubation_mm,
                "precip_recent_4d_mm": precip_recent_4d_mm,
                "rain_timing_status": rain_timing_status,
                "precip_last_3d_mm": cell_meteo_data.get("precip_last_3d_mm"),
                "precip_score": round(precip_pts, 1),
                "temp_score": round(temp_pts, 1),
                "soil_moisture_score": round(soil_pts, 1),
                "thermal_shock_adj": round(thermal_shock_adj, 1),
                "humidity_adj": round(humidity_adj, 1),
                "wind_penalty_pct": round(wind_penalty * 100.0, 1),
                "slope_score": round(slope_pts, 1),
                "aspect_score": round(aspect_pts, 1),
                "temp_mean_c": temp_mean,
                "temp_recent_3d_c": temp_recent_3d,
                "temp_min_recent_3d_c": temp_min_recent,
                "soil_temp_mean_c": soil_temp,
                "humidity_mean_7d_pct": humidity_7d,
                "water_balance_14d_mm": water_balance,
                "thermal_shock_celsius": thermal_shock,
                "thermal_shock_status": thermal_shock_status,
                "wind_max_14d_kmh": wind_max,
                "phenology_factor": phenology_factor,
                "season_status": season_status,
                "soil_ph": soil_ph,
                "effective_soil_ph": effective_soil_ph,
                "host_tree": dominant_tree
            }
        )
        if relative_mode:
            result["weather_score"] = weather_score
        return result

    def _soil_texture_score(
        self, sand_percent: float, silt_percent: float, clay_percent: float
    ) -> float:
        """Puntúa la textura sin asignar preferencias específicas por especie."""
        total = sand_percent + silt_percent + clay_percent
        sand_fraction = sand_percent / total * 100
        clay_fraction = clay_percent / total * 100
        texture_rules = self.config["soil_texture_scoring"]

        def component_score(value: float, penalty_rules: Dict[str, float]) -> float:
            start = penalty_rules["start_percent"]
            zero = penalty_rules["zero_percent"]
            if value <= start:
                return 1.0
            return max(0.0, 1.0 - (value - start) / (zero - start))

        silt_fraction = silt_percent / total * 100
        balanced_fraction = sum((
            component_score(sand_fraction, texture_rules["sand_penalty"]),
            component_score(silt_fraction, texture_rules["silt_penalty"]),
            component_score(clay_fraction, texture_rules["clay_penalty"]),
        )) / 3
        return texture_rules["max_points"] * balanced_fraction

    def _static_habitat_exclusion(
        self,
        species_id: str,
        cell_static_data: Dict[str, Any],
        allow_unknown_host: bool,
    ) -> tuple[bool | None, str | None]:
        rules = self.species_matrix[species_id]
        habitat = rules["habitat"]
        edaphic = rules["edaphic"]
        dominant_tree = cell_static_data["dominant_species"]
        canopy_cover = cell_static_data["canopy_cover"]
        elevation = cell_static_data["elevation_m"]
        effective_ph = self._effective_soil_ph(cell_static_data)

        host_assessment = self._assess_host(
            dominant_tree, habitat["preferred_forest_types"]
        )
        if host_assessment is False or (
            host_assessment is None and not allow_unknown_host
        ):
            return host_assessment, (
                "Exclusión: Especie forestal no hospedadora para micorriza"
            )
        if canopy_cover < habitat.get("minimum_canopy_cover_percent", 20):
            return host_assessment, "Exclusión: Cobertura de copa insuficiente"

        alt_min, alt_max = habitat["altitude_range_meters"]
        if elevation < (alt_min - 150) or elevation > (alt_max + 150):
            return host_assessment, (
                "Exclusión: Fuera del rango bioclimático altitudinal"
            )

        ph_tol_min, ph_tol_max = edaphic["ph_tolerated"]
        if effective_ph < ph_tol_min or effective_ph > ph_tol_max:
            return host_assessment, (
                f"Exclusión: pH {effective_ph} incompatible "
                f"(Tolerado: {ph_tol_min}-{ph_tol_max})"
            )
        return host_assessment, None

    @staticmethod
    def _assess_host(
        dominant_tree: str, preferred_forest_types: list[str]
    ) -> bool | None:
        normalized = unicodedata.normalize("NFKD", dominant_tree.casefold())
        normalized = "".join(
            char for char in normalized if not unicodedata.combining(char)
        )
        normalized = " ".join(normalized.split())
        host_aliases = {
            "Pinus sylvestris": (
                "pino albar",
                "pinus sylvestris",
                "mezclas de coniferas autoctonas",
            ),
            "Pinus nigra": ("pino salgareno", "pino laricio", "pinus nigra"),
            "Pinus pinaster": (
                "pino pinaster",
                "pinus pinaster",
                "mezclas de coniferas autoctonas",
            ),
            "Pinus pinea": ("pino pinonero", "pinus pinea"),
            "Pinus halepensis": ("pino carrasco", "pinus halepensis"),
            "Pinus uncinata": ("pino negro", "pinus uncinata"),
            "Fagus sylvatica": ("hayedo", "haya", "fagus sylvatica"),
            "Quercus pyrenaica": (
                "melojar",
                "melojares",
                "quercus pyrenaica",
                "bosques mixtos de frondosas",
            ),
            "Quercus petraea": (
                "roble albar",
                "quercus petraea",
                "quercus petrae",
                "bosques mixtos de frondosas",
            ),
            "Quercus robur": (
                "roble carballo",
                "quercus robur",
                "bosques mixtos de frondosas",
            ),
            "Quercus faginea": (
                "quejigar",
                "quejigo",
                "quercus faginea",
                "bosques mixtos de frondosas",
            ),
            "Quercus ilex": ("encinar", "encina", "quercus ilex"),
            "Quercus suber": ("alcornocal", "alcornoque", "quercus suber"),
            "Castanea sativa": ("castanar", "castano", "castanea sativa"),
            "Betula alba": ("abedular", "abedul", "betula alba"),
            "Non_Mycorrhizal_Or_Non_Boletus": (
                "bosques riberenos",
                "sabinares",
                "juniperus",
                "choperas",
                "plataneras",
                "enebrales",
                "fresnedas",
                "acebedas",
                "acebuchares",
                "frondosas aloctonas invasoras",
                "arbolado disperso de frondosas",
                "coniferas aloctonas de gestion",
                "otras coniferas aloctonas de produccion",
            ),
        }
        for preferred in preferred_forest_types:
            aliases = host_aliases.get(preferred, (preferred.casefold(),))
            for alias in aliases:
                decomposed_alias = unicodedata.normalize("NFKD", alias.casefold())
                decomposed_alias = "".join(
                    char for char in decomposed_alias if not unicodedata.combining(char)
                )
                if decomposed_alias in normalized:
                    return True
        for aliases in host_aliases.values():
            for alias in aliases:
                decomposed_alias = unicodedata.normalize("NFKD", alias.casefold())
                decomposed_alias = "".join(
                    char for char in decomposed_alias if not unicodedata.combining(char)
                )
                if decomposed_alias in normalized:
                    return False
        return None

    @staticmethod
    def _is_compatible_host(dominant_tree: str, preferred_forest_types: list[str]) -> bool:
        return MushroomScoringEngine._assess_host(
            dominant_tree, preferred_forest_types
        ) is True

    def _build_result(self, score: float, reason: str, breakdown: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "score": score,
            "status": (
                "experimental_favorable" if score >= 50
                else "experimental_intermediate" if score >= 25
                else "experimental_low_or_excluded"
            ),
            "reason": reason,
            "breakdown": breakdown or {}
        }
