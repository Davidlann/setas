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
    def _smooth_trapezoid(
        value: float, low_zero: float, opt_min: float, opt_max: float, high_zero: float
    ) -> float:
        """Función trapezoidal continua en [0.0, 1.0]."""
        if opt_min <= value <= opt_max:
            return 1.0
        if value < opt_min:
            if value <= low_zero:
                return 0.0
            return (value - low_zero) / max(1e-6, opt_min - low_zero)
        if value >= high_zero:
            return 0.0
        return (high_zero - value) / max(1e-6, high_zero - opt_max)

    @staticmethod
    def _normalize_text(text: str) -> str:
        normalized = unicodedata.normalize("NFKD", text.casefold())
        normalized = "".join(
            char for char in normalized if not unicodedata.combining(char)
        )
        return " ".join(normalized.split())

    @classmethod
    def _host_leaf_habit(cls, dominant_tree: str) -> str:
        """Clasifica el hábito foliar del hospedador (evergreen_conifer, evergreen_broadleaf, deciduous, mixed)."""
        normalized = cls._normalize_text(dominant_tree)
        if "coniferas y frondosas" in normalized or "frondosas con" in normalized:
            return "mixed"
        if any(
            token in normalized
            for token in (
                "pinus", "pino", "coniferas autoctonas", "coniferas aloctonas",
                "sabinar", "enebral", "juniperus",
            )
        ):
            return "evergreen_conifer"
        if any(
            token in normalized
            for token in (
                "quercus ilex", "encina", "quercus suber", "alcornoque",
                "acebeda", "acebuchar",
            )
        ):
            return "evergreen_broadleaf"
        if any(
            token in normalized
            for token in (
                "quercus pyrenaica", "melojar", "quercus petrae", "roble",
                "quercus robur", "quercus faginea", "quejigar", "quejigo",
                "fagus", "hayedo", "haya", "castanea", "castanar", "castano",
                "betula", "abedul", "frondosas", "chopera", "riberen", "fresneda",
            )
        ):
            return "deciduous"
        return "mixed"

    @classmethod
    def _effective_soil_ph(cls, cell_static_data: Dict[str, Any]) -> float:
        """Calcula el pH efectivo en humus amortiguado por litología, carbonatos y hojarasca."""
        soil_ph = float(cell_static_data["soil_ph"])
        ph_q05 = cell_static_data.get("soil_ph_q05")
        litho = str(cell_static_data.get("lithology_class") or "").strip().lower()
        if (
            isinstance(ph_q05, (int, float))
            and math.isfinite(ph_q05)
            and 0.0 <= ph_q05 <= soil_ph
        ):
            dominant_tree = str(cell_static_data.get("dominant_species") or "")
            leaf_habit = cls._host_leaf_habit(dominant_tree)
            norm_tree = cls._normalize_text(dominant_tree)
            is_acid_litter = (
                leaf_habit == "evergreen_conifer"
                or "fagus" in norm_tree
                or "haya" in norm_tree
                or "hayedo" in norm_tree
                or "castan" in norm_tree
            )
            if litho == "siliceous" and soil_ph >= 6.5:
                q_weight = 0.32
            elif soil_ph >= 6.8:
                q_weight = 0.22 if leaf_habit == "evergreen_conifer" else 0.15
            else:
                q_weight = 0.30 if is_acid_litter else 0.22
            eff_ph = round(soil_ph * (1.0 - q_weight) + float(ph_q05) * q_weight, 2)
            if litho == "calcareous" and soil_ph >= 6.8:
                eff_ph = max(eff_ph, round(max(6.4, soil_ph - 0.35), 2))
            return eff_ph
        return soil_ph

    @staticmethod
    def _topographic_solar_factor(
        latitude: Optional[float],
        slope_deg: Optional[float],
        aspect_cardinal: Optional[str],
        snapshot_date: Optional[str] = None,
    ) -> float:
        """Calcula la razón de insolación al mediodía sobre la ladera frente al plano horizontal."""
        if (
            not isinstance(slope_deg, (int, float))
            or not math.isfinite(slope_deg)
            or float(slope_deg) < 1.0
            or not aspect_cardinal
        ):
            return 1.0
        az_map = {
            "N": 0.0, "NE": 45.0, "E": 90.0, "SE": 135.0,
            "S": 180.0, "SW": 225.0, "SO": 225.0, "W": 270.0, "O": 270.0,
            "NW": 315.0, "NO": 315.0,
        }
        az_deg = az_map.get(str(aspect_cardinal).strip().upper())
        if az_deg is None:
            return 1.0
        lat_deg = (
            float(latitude)
            if isinstance(latitude, (int, float)) and math.isfinite(latitude)
            else 40.9
        )
        doy = 285
        if snapshot_date:
            try:
                doy = date.fromisoformat(str(snapshot_date)[:10]).timetuple().tm_yday
            except ValueError:
                pass
        decl_deg = 23.44 * math.sin(2.0 * math.pi * (doy - 81) / 365.0)
        zenith_rad = math.radians(max(3.0, min(85.0, abs(lat_deg - decl_deg))))
        slope_rad = math.radians(max(0.0, min(60.0, float(slope_deg))))
        az_rad = math.radians(az_deg)
        cos_z = math.cos(zenith_rad)
        cos_i = (
            math.cos(slope_rad) * cos_z
            - math.sin(slope_rad) * math.cos(az_rad) * math.sin(zenith_rad)
        )
        direct_ratio = max(0.05, cos_i) / max(0.15, cos_z)
        # 25% componente difusa del cielo + 75% radiación directa geométrica
        eff_ratio = 0.25 + 0.75 * direct_ratio
        return round(max(0.55, min(1.35, eff_ratio)), 2)

    @staticmethod
    def _compute_phenology_factor(
        meteo_rules: Dict[str, Any],
        snapshot_date: Optional[str],
        elevation_m: Optional[float] = None,
        leaf_habit: Optional[str] = None,
        hard_frost_28d: bool = False,
    ) -> tuple[float, Optional[str]]:
        """Calcula el factor fenológico estacional (0.05–1.0) según fecha, altitud y dormancia foliar."""
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
                alt_penalty = min(0.38, (elevation_m - 1300.0) / 1500.0)
                base_factor = max(0.15, base_factor - alt_penalty)
            elif month in (8, 9) and elevation_m >= 1400:
                base_factor = min(1.0, base_factor + 0.12)

        # Dormancia invernal en frondosas caducifolias tras caída foliar y heladas de noviembre
        if leaf_habit == "deciduous" and (
            month in (12, 1, 2, 3) or (month == 11 and parsed.day >= 10)
        ):
            if month == 11:
                days_past = parsed.day - 10
                dormancy = max(0.35, 1.0 - days_past * 0.032)
            elif month in (12, 1, 2):
                dormancy = 0.35
            else:
                dormancy = 0.50
            if hard_frost_28d and (
                not isinstance(elevation_m, (int, float)) or elevation_m >= 900
            ):
                dormancy *= 0.72
            base_factor *= dormancy

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
        result["combined_score"] = (
            round((result["score"] ** 0.40) * (result["weather_score"] ** 0.60), 1)
            if result["score"] > 0 and result["weather_score"] > 0
            else 0.0
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
        leaf_habit = self._host_leaf_habit(dominant_tree)

        host_assessment, host_tier, host_multiplier, exclusion_reason = (
            self._static_habitat_assessment(
                species_id, cell_static_data, allow_unknown_host=relative_mode
            )
        )
        host_details = {
            "host_assessment": (
                "compatible" if host_assessment is True
                else "unknown" if host_assessment is None
                else "incompatible"
            ),
            "host_tier": host_tier,
            "host_leaf_habit": leaf_habit,
            "host_tree": dominant_tree,
        }
        if exclusion_reason is not None and not relative_mode:
            return self._build_result(
                0.0, exclusion_reason, breakdown=host_details
            )

        # ==============================================================
        # 2. PONDERACIÓN METEOROLÓGICA ACOPLADA (0–100 y equivalente 0–45)
        # ==============================================================
        meteo_rules = rules["meteorology"]
        precip_14d = cell_meteo_data["precip_accum_14d_mm"]
        temp_mean = cell_meteo_data["temp_mean_14d_celsius"]
        temp_min = cell_meteo_data["temp_min_14d_celsius"]
        soil_moisture = cell_meteo_data["soil_moisture_percent"]

        thresh_precip = meteo_rules["accumulated_precip_14d_threshold_mm"]
        opt_precip = meteo_rules["accumulated_precip_14d_optimal_mm"]
        lag_profile_name = meteo_rules.get("precipitation_lag_profile", "autumn_cool")
        lag_weights = self.PRECIPITATION_LAG_PROFILES.get(
            lag_profile_name, self.PRECIPITATION_LAG_WEIGHTS
        )

        daily_p28 = cell_meteo_data.get("precip_daily_28d_mm")
        daily_st28 = cell_meteo_data.get("soil_temp_daily_28d_c")
        daily_tmin28 = cell_meteo_data.get("temp_min_daily_28d_c")

        t_lo = float(meteo_rules.get("soil_temp_min_cutoff_celsius", 5.0))
        t_hi = float(meteo_rules.get("soil_temp_max_cutoff_celsius", 24.0))
        ref_speed_t = float(meteo_rules.get("thermal_speed_ref_celsius", 13.0))

        eff_rain_active = precip_14d
        eff_rain_tail = 0.0
        precip_incubation_mm = None
        precip_recent_4d_mm = cell_meteo_data.get("precip_last_3d_mm")
        rain_timing_status = None
        used_thermal_time_28d = False

        if (
            isinstance(daily_p28, list)
            and len(daily_p28) == 28
            and isinstance(daily_st28, list)
            and len(daily_st28) == 28
        ):
            used_thermal_time_28d = True
            cum_td = 0.0
            eff_rain_active = 0.0
            eff_rain_tail = 0.0
            for age in range(28):
                day_idx = 27 - age
                day_st = float(daily_st28[day_idx])
                speed = max(
                    0.10,
                    min(1.25, (day_st - t_lo) / max(2.0, ref_speed_t - t_lo)),
                )
                if age > 0:
                    cum_td += speed
                rain_day = float(daily_p28[day_idx])
                if 3.5 <= cum_td <= 15.5:
                    eff_rain_active += rain_day * self._smooth_trapezoid(
                        cum_td, 3.5, 6.5, 11.5, 15.5
                    )
                if 10.5 <= cum_td <= 24.5:
                    eff_rain_tail += rain_day * self._smooth_trapezoid(
                        cum_td, 10.5, 13.0, 19.0, 24.5
                    )
            series_14 = daily_precipitation if daily_precipitation is not None else daily_p28[-14:]
            precip_incubation_mm = round(sum(series_14[:10]), 2)
            precip_recent_4d_mm = round(sum(series_14[10:]), 2)
            if eff_rain_active >= thresh_precip * 0.75:
                rain_timing_status = "optimal_incubation"
            elif eff_rain_tail >= thresh_precip * 0.38:
                rain_timing_status = "mature_tail"
            elif (precip_recent_4d_mm or 0.0) >= thresh_precip * 0.40:
                rain_timing_status = "too_recent_for_flush"
            elif eff_rain_active >= thresh_precip * 0.35:
                rain_timing_status = "moderate"
            else:
                rain_timing_status = "dry"
        elif daily_precipitation is not None:
            weight_mean = sum(lag_weights) / len(lag_weights)
            eff_rain_active = sum(
                rain * weight
                for rain, weight in zip(reversed(daily_precipitation), lag_weights)
            ) / weight_mean
            precip_incubation_mm = round(sum(daily_precipitation[:10]), 2)
            precip_recent_4d_mm = round(sum(daily_precipitation[10:]), 2)
            if precip_incubation_mm < thresh_precip * 0.25:
                eff_rain_active = min(eff_rain_active, thresh_precip * 0.55)
                rain_timing_status = (
                    "too_recent_for_flush"
                    if precip_recent_4d_mm >= thresh_precip * 0.35
                    else "dry"
                )
            elif precip_incubation_mm < thresh_precip * 0.75:
                max_allowed = min(opt_precip * 0.85, precip_incubation_mm * 2.2)
                if eff_rain_active > max_allowed:
                    eff_rain_active = max_allowed
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

        # Ajuste por balance hídrico (evapotranspiración)
        water_balance = cell_meteo_data.get("water_balance_14d_mm")
        if water_balance is not None:
            if water_balance < 0:
                drying_factor = max(0.75, 1.0 + (water_balance / 140.0))
                eff_rain_active *= drying_factor
                eff_rain_tail *= drying_factor
            elif water_balance > 25.0:
                eff_rain_active = min(opt_precip * 1.2, eff_rain_active * 1.05)

        def rain_curve(rain_mm: float) -> float:
            if rain_mm < thresh_precip:
                return (rain_mm / thresh_precip) * 0.35
            if rain_mm >= opt_precip:
                return 1.0
            return 0.35 + 0.65 * (rain_mm - thresh_precip) / max(
                1.0, opt_precip - thresh_precip
            )

        active_rain_idx = rain_curve(eff_rain_active)
        tail_rain_idx = rain_curve(eff_rain_tail) * 0.68
        rain_idx = (
            max(active_rain_idx, min(1.0, active_rain_idx * 0.80 + tail_rain_idx * 0.45))
            if used_thermal_time_28d
            else active_rain_idx
        )

        # Arrastre de sequía prolongada previa a las lluvias
        dry_streak = cell_meteo_data.get("pre_rain_dry_streak_days")
        pre_rain_sm = cell_meteo_data.get("pre_rain_min_soil_moisture_pct")
        drought_factor = 1.0
        if (
            isinstance(dry_streak, (int, float))
            and isinstance(pre_rain_sm, (int, float))
            and math.isfinite(pre_rain_sm)
            and dry_streak >= 11
            and pre_rain_sm < 19.5
        ):
            drought_factor = 0.72 + 0.28 * max(
                0.0, min(1.0, (float(pre_rain_sm) - 13.0) / 6.5)
            )

        # Penalización por encharcamiento frío (anoxia radicular)
        soil_temp = cell_meteo_data.get("soil_temp_mean_14d_celsius")
        waterlog_factor = 1.0
        if (
            soil_moisture >= 37.5
            and soil_temp is not None
            and soil_temp < 10.5
            and precip_14d >= 70.0
        ):
            waterlog_factor = max(0.70, 1.0 - (soil_moisture - 36.5) * 0.05)

        min_soil_m = meteo_rules.get("soil_moisture_min_percent", 20.0)
        sm_idx = min(1.0, soil_moisture / max(1.0, min_soil_m + 2.0))
        moisture_index = (
            min(1.0, rain_idx * 0.82 + sm_idx * 0.18)
            * drought_factor
            * waterlog_factor
        )
        precip_pts = rain_idx * 25.0 * drought_factor * waterlog_factor
        soil_pts = sm_idx * 5.0

        # Índice térmico continuo (0..1) usando temperatura radicular + aérea
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

        thermal_index = self._smooth_trapezoid(
            eval_temp, t_lo, t_opt_min, t_opt_max, t_hi
        )

        st07_recent = cell_meteo_data.get("soil_temp_0_7cm_3d_celsius")
        if (
            isinstance(st07_recent, (int, float))
            and math.isfinite(st07_recent)
            and st07_recent < t_lo + 0.5
        ):
            thermal_index *= max(
                0.20,
                self._smooth_trapezoid(
                    float(st07_recent), t_lo - 2.0, t_lo + 1.0, 25.0, 30.0
                ),
            )

        # Penalización graduada por heladas recientes y días con helada
        min_limit = float(meteo_rules.get("min_temp_limit_celsius", 0.0))
        temp_min_recent = cell_meteo_data.get("temp_min_recent_3d_celsius")
        has_recent_min = (
            isinstance(temp_min_recent, (int, float))
            and math.isfinite(temp_min_recent)
        )
        frost_days_species_7d = 0
        if isinstance(daily_tmin28, list) and len(daily_tmin28) == 28:
            frost_days_species_7d = sum(
                1 for t_val in daily_tmin28[-7:] if t_val < min_limit
            )
        elif (
            cell_meteo_data.get("frost_days_7d") is not None
            and abs(min_limit - 0.5) < 0.6
        ):
            frost_days_species_7d = int(cell_meteo_data["frost_days_7d"])

        if has_recent_min:
            if float(temp_min_recent) < min_limit:
                severity = min(1.0, (min_limit - float(temp_min_recent)) / 3.0)
                thermal_index *= max(0.10, 0.45 - 0.35 * severity)
            elif frost_days_species_7d >= 2:
                thermal_index *= 0.55
            elif temp_min < min_limit - 2.0:
                thermal_index *= 0.75
            elif temp_min < min_limit:
                thermal_index *= 0.88
        else:
            if temp_min < min_limit:
                severity = min(1.0, (min_limit - float(temp_min)) / 3.0)
                thermal_index *= max(0.10, 0.45 - 0.35 * severity)
        temp_pts = thermal_index * 15.0

        # Choque térmico biológicamente coherente
        thermal_shock = cell_meteo_data.get("thermal_shock_celsius")
        thermal_shock_status = None
        thermal_shock_adj = 0.0
        shock_mult = 1.0
        if thermal_shock is not None:
            shock_trigger = meteo_rules.get("thermal_shock_trigger")
            recent_above_frost = (
                (not has_recent_min or float(temp_min_recent) >= min_limit + 1.0)
                and (has_recent_min or temp_min >= min_limit + 1.0)
            )
            if shock_trigger is True:
                if (
                    thermal_shock <= -2.0
                    and eff_rain_active >= thresh_precip
                    and eval_temp >= t_opt_min - 1.5
                    and recent_above_frost
                ):
                    shock_mult = 1.07
                    thermal_shock_adj = 2.5
                    thermal_shock_status = "triggered_favorable"
                elif thermal_shock <= -2.5 and eval_temp < t_opt_min - 2.0:
                    shock_mult = 0.88
                    thermal_shock_adj = -3.0
                    thermal_shock_status = "cold_snap_stalled"
                elif thermal_shock > 2.0 and eval_temp > t_opt_max - 1.5:
                    shock_mult = 0.92
                    thermal_shock_adj = -2.0
                    thermal_shock_status = "warming_inhibited"
                else:
                    thermal_shock_status = "stable"
            elif shock_trigger is False:
                if thermal_shock <= -3.5:
                    shock_mult = 0.85
                    thermal_shock_adj = -4.0
                    thermal_shock_status = "cold_suppressed"
                elif thermal_shock >= 0.0 and eval_temp >= t_opt_min - 1.0:
                    thermal_shock_status = "warm_favorable"
                else:
                    thermal_shock_status = "stable"

        # Ajuste por humedad relativa ambiental reciente
        humidity_7d = cell_meteo_data.get("humidity_mean_7d_percent")
        humidity_adj = 0.0
        rh_mult = 1.0
        if isinstance(humidity_7d, (int, float)) and math.isfinite(humidity_7d):
            if humidity_7d >= 72.0 and eff_rain_active >= thresh_precip * 0.6:
                rh_mult = 1.0 + min(0.05, (humidity_7d - 72.0) / 360.0)
                humidity_adj = min(2.0, (humidity_7d - 72.0) / 10.0)
            elif humidity_7d < 45.0:
                rh_mult = 1.0 - min(0.12, (45.0 - humidity_7d) / 200.0)
                humidity_adj = -min(2.5, (45.0 - humidity_7d) / 8.0)

        # Factor viento (desecación de primordios en cumbres/laderas)
        wind_max = cell_meteo_data.get("wind_max_14d_kmh")
        wind_penalty = 0.0
        if wind_max is not None:
            wind_limit = meteo_rules.get("wind_max_limit_kmh", 45.0)
            if wind_max > wind_limit:
                wind_penalty = min(0.20, (wind_max - wind_limit) / 100.0)
        wind_mult = 1.0 - wind_penalty

        # Factor fenológico estacional consciente de la especie arbórea y heladas previas
        snapshot_date = cell_meteo_data.get("snapshot_date")
        hard_frost_28d = bool(cell_meteo_data.get("hard_frost_28d")) or (temp_min <= -1.0)
        phenology_factor, season_status = self._compute_phenology_factor(
            meteo_rules,
            snapshot_date,
            elevation_m=elevation,
            leaf_habit=leaf_habit,
            hard_frost_28d=hard_frost_28d,
        )

        # Factor nieve reciente en suelo frío
        snowfall_7d = cell_meteo_data.get("snowfall_7d_cm")
        snowfall_3d_fc = cell_meteo_data.get("snowfall_3d_fc_cm")
        snow_factor = 1.0
        if (
            isinstance(snowfall_7d, (int, float))
            and math.isfinite(snowfall_7d)
            and float(snowfall_7d) >= 3.0
            and air_temp_ref < 5.5
        ):
            snow_factor = round(max(0.35, 1.0 - (float(snowfall_7d) - 2.0) * 0.06), 2)

        # Factor radiación solar topográfica
        rad_7d = cell_meteo_data.get("radiation_mean_7d_mj")
        solar_factor = self._topographic_solar_factor(
            cell_static_data.get("latitude"),
            cell_static_data.get("slope_deg"),
            aspect_norm,
            snapshot_date=snapshot_date,
        )
        solar_weather_mult = 1.0
        rad_topo_adj = 0.0
        if aspect_norm is not None:
            has_sun_energy = (rad_7d is None) or (
                isinstance(rad_7d, (int, float)) and math.isfinite(rad_7d) and float(rad_7d) >= 5.0
            )
            if eval_temp < t_opt_min and has_sun_energy:
                if solar_factor > 1.0:
                    rad_topo_adj = min(2.5, (solar_factor - 1.0) * 9.0)
                    solar_weather_mult = min(1.06, 1.0 + (solar_factor - 1.0) * 0.22)
                elif solar_factor < 1.0:
                    rad_topo_adj = max(-2.5, (solar_factor - 1.0) * 8.0)
                    solar_weather_mult = max(0.92, 1.0 + (solar_factor - 1.0) * 0.25)
            elif eval_temp > (t_opt_max - 1.5) or (
                water_balance is not None and water_balance < -10.0
            ):
                if solar_factor < 1.0:
                    rad_topo_adj = min(2.5, (1.0 - solar_factor) * 8.0)
                    solar_weather_mult = min(1.05, 1.0 + (1.0 - solar_factor) * 0.18)
                elif solar_factor > 1.05:
                    rad_topo_adj = max(-2.5, -(solar_factor - 1.0) * 8.0)
                    solar_weather_mult = max(0.93, 1.0 - (solar_factor - 1.0) * 0.22)

        # Acoplamiento multiplicativo (Ley del Mínimo)
        raw_meteo = (moisture_index ** 0.52) * (thermal_index ** 0.48)
        coupled_meteo = min(
            raw_meteo, min(moisture_index, thermal_index) * 1.45 + 0.10
        )
        weather_unit = max(
            0.0,
            min(
                1.0,
                coupled_meteo
                * shock_mult
                * rh_mult
                * wind_mult
                * snow_factor
                * solar_weather_mult
                * phenology_factor,
            ),
        )
        weather_score = min(100.0, round(weather_unit * 100.0, 1))
        score_meteo = round(weather_unit * 45.0, 1)

        # Clasificador de fase de brotada (flush_phase) y previsión 3d (forecast_outlook)
        parsed_snap_date = None
        if snapshot_date:
            try:
                parsed_snap_date = date.fromisoformat(str(snapshot_date)[:10])
            except ValueError:
                parsed_snap_date = None

        tmin_eval = float(temp_min_recent) if has_recent_min else float(temp_min)
        recent_4d_val = float(precip_recent_4d_mm or 0.0)
        heavy_snow_cold = (
            isinstance(snowfall_7d, (int, float))
            and math.isfinite(snowfall_7d)
            and float(snowfall_7d) >= 8.0
            and air_temp_ref < 3.5
        )
        if exclusion_reason is not None:
            flush_phase = "excluded"
        elif phenology_factor < 0.30:
            flush_phase = "off_season"
        elif (
            leaf_habit == "deciduous"
            and parsed_snap_date is not None
            and (
                parsed_snap_date.month in (12, 1, 2)
                or (parsed_snap_date.month == 11 and parsed_snap_date.day >= 12)
            )
            and hard_frost_28d
        ):
            flush_phase = "dormant_host"
        elif (
            tmin_eval < min_limit
            or (st07_recent is not None and float(st07_recent) < t_lo)
            or thermal_index < 0.25
            or heavy_snow_cold
        ):
            flush_phase = "cold_stalled"
        elif (
            eff_rain_active >= thresh_precip * 0.85
            and thermal_index >= 0.55
            and drought_factor >= 0.85
            and eff_rain_tail >= thresh_precip * 0.40
        ):
            flush_phase = "peak_flush"
        elif (
            eff_rain_active >= thresh_precip * 0.75
            and thermal_index >= 0.50
            and drought_factor >= 0.85
        ):
            flush_phase = "emerging_buttons"
        elif eff_rain_tail >= thresh_precip * 0.38 or (
            eff_rain_active >= thresh_precip * 0.60 and thermal_index < 0.50
        ):
            flush_phase = "mature_tail"
        elif rain_timing_status == "too_recent_for_flush" or (
            recent_4d_val >= thresh_precip * 0.45 and eff_rain_active < thresh_precip * 0.60
        ):
            flush_phase = "incubating"
        elif max(eff_rain_active, eff_rain_tail) < thresh_precip * 0.40:
            flush_phase = "dry"
        else:
            flush_phase = "moderate_active"

        fc_precip = cell_meteo_data.get("forecast_3d_precip_mm")
        fc_tmin = cell_meteo_data.get("forecast_3d_temp_min_celsius")
        fc_tmean = cell_meteo_data.get("forecast_3d_temp_mean_celsius")
        forecast_outlook = None
        if any(v is not None for v in (fc_precip, fc_tmin, fc_tmean, snowfall_3d_fc)):
            if snowfall_3d_fc is not None and math.isfinite(snowfall_3d_fc) and float(snowfall_3d_fc) >= 3.0:
                forecast_outlook = "snow_risk"
            elif fc_tmin is not None and float(fc_tmin) < min_limit:
                forecast_outlook = "frost_risk"
            elif fc_precip is not None and float(fc_precip) >= 12.0:
                forecast_outlook = "rain_incoming"
            elif (
                fc_tmean is not None
                and float(fc_tmean) >= t_opt_min
                and eval_temp < t_opt_min
            ):
                forecast_outlook = "warming_favorable"
            else:
                forecast_outlook = "stable"

        # ==============================================================
        # 3. PONDERACIÓN EDAFOLÓGICA (Máximo 35 puntos)
        # ==============================================================
        ph_opt_min, ph_opt_max = edaphic["ph_optimal"]
        ph_tol_min, ph_tol_max = edaphic["ph_tolerated"]
        if ph_opt_min <= effective_soil_ph <= ph_opt_max:
            score_ph = 35.0
        else:
            if effective_soil_ph < ph_opt_min:
                dist = ph_opt_min - effective_soil_ph
                max_d = ph_opt_min - ph_tol_min
            else:
                dist = effective_soil_ph - ph_opt_max
                max_d = ph_tol_max - ph_opt_max

            ratio = max(0.0, 1.0 - (dist / max(0.1, max_d)))
            score_ph = 12.0 + ratio * 23.0

        texture_score = None
        if texture_is_complete:
            texture_score = self._soil_texture_score(
                cell_static_data["soil_sand_percent"],
                cell_static_data["soil_silt_percent"],
                cell_static_data["soil_clay_percent"],
            )
        soil_soc = cell_static_data.get("soil_soc_g_kg")
        soc_score = None
        if isinstance(soil_soc, (int, float)) and math.isfinite(soil_soc):
            soc_score = self._soil_soc_score(float(soil_soc))

        if texture_score is not None and soc_score is not None:
            score_edaphic = score_ph * (25.0 / 35.0) + texture_score + soc_score
        elif texture_score is not None:
            score_edaphic = score_ph * (28.0 / 35.0) + texture_score
        elif soc_score is not None:
            score_edaphic = score_ph * (32.0 / 35.0) + soc_score
        else:
            score_edaphic = score_ph

        lithology_class = str(cell_static_data.get("lithology_class") or "").strip().lower()
        lithology_desc = cell_static_data.get("lithology_desc")
        substrate_pref = str(edaphic.get("substrate_preference") or "").casefold()
        litho_adj = 0.0
        if "indifferent" in substrate_pref:
            if lithology_class in ("siliceous", "calcareous"):
                litho_adj = 0.5
        elif "siliceous" in substrate_pref:
            if lithology_class == "siliceous":
                litho_adj = 2.0
            elif lithology_class == "calcareous":
                litho_adj = -3.5
        score_edaphic = max(0.0, min(35.0, score_edaphic + litho_adj))

        # ==============================================================
        # 4. PONDERACIÓN TOPOGRÁFICA (Máximo 20 puntos)
        # ==============================================================
        topo_rules = rules["topography"]
        slope = cell_static_data["slope_deg"]
        aspect = aspect_norm
        tpi_val = cell_static_data.get("tpi_m")

        max_slope = topo_rules.get("slope_max_deg", 45)
        slope_raw = (
            1.0
            if slope <= (max_slope * 0.7)
            else max(0.0, 1.0 - (slope / max_slope))
        )

        pref_aspects = (
            topo_rules.get("aspect_preference_early_season", [])
            if season_phase == "early"
            else topo_rules.get("aspect_preference_late_season", [])
        )
        aspect_raw = 1.0 if aspect in pref_aspects else 0.5

        tpi_score = None
        if isinstance(tpi_val, (int, float)) and math.isfinite(tpi_val):
            slope_pts = slope_raw * 9.0
            aspect_pts = aspect_raw * 8.0
            tpi_f = float(tpi_val)
            if tpi_f <= -18.0:
                tpi_score = 1.8 if tmin_eval <= 1.0 else 2.6
            elif tpi_f <= 4.0:
                tpi_score = 3.0
            elif tpi_f <= 18.0:
                tpi_score = 3.0 - ((tpi_f - 4.0) / 14.0) * 1.4
            else:
                tpi_score = 1.2
            score_topo = slope_pts + aspect_pts + tpi_score
        else:
            slope_pts = slope_raw * 10.0
            aspect_pts = aspect_raw * 10.0
            score_topo = slope_pts + aspect_pts
        score_topo = max(0.0, min(20.0, score_topo + rad_topo_adj))

        # ==============================================================
        # TOTAL SCORE (0 a 100)
        # ==============================================================
        if relative_mode:
            habitat_rules = rules["habitat"]
            habitat_raw = 100.0 * ((score_edaphic + score_topo) / 55.0)
            cc_min = float(habitat_rules.get("minimum_canopy_cover_percent", 20))
            cc_opt = habitat_rules.get("optimal_canopy_cover_percent", [40, 80])
            if cc_opt[0] <= canopy_cover <= cc_opt[1]:
                canopy_factor = 1.0
            elif canopy_cover < cc_opt[0]:
                canopy_factor = 0.80 + 0.20 * max(
                    0.0, (canopy_cover - cc_min) / max(1.0, cc_opt[0] - cc_min)
                )
            else:
                canopy_factor = max(0.92, 1.0 - (canopy_cover - cc_opt[1]) / 250.0)

            alt_lim = habitat_rules["altitude_range_meters"]
            alt_opt = habitat_rules.get("optimal_altitude_range_meters", alt_lim)
            if alt_opt[0] <= elevation <= alt_opt[1]:
                alt_factor = 1.0
            elif elevation < alt_opt[0]:
                span = max(1.0, alt_opt[0] - (alt_lim[0] - 150.0))
                alt_factor = 0.80 + 0.20 * max(
                    0.0, 1.0 - (alt_opt[0] - elevation) / span
                )
            else:
                span = max(1.0, (alt_lim[1] + 150.0) - alt_opt[1])
                alt_factor = 0.80 + 0.20 * max(
                    0.0, 1.0 - (elevation - alt_opt[1]) / span
                )

            total_score = habitat_raw * canopy_factor * alt_factor * host_multiplier
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
                "soil_soc_g_kg": soil_soc,
                "soil_soc_score": round(soc_score, 1) if soc_score is not None else None,
                "tpi_m": tpi_val,
                "tpi_score": round(tpi_score, 1) if tpi_score is not None else None,
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
                "host_tier": host_tier,
                "host_leaf_habit": leaf_habit,
                "flush_phase": flush_phase,
                "forecast_outlook": forecast_outlook,
                "forecast_3d_precip_mm": fc_precip,
                "forecast_3d_temp_min_celsius": fc_tmin,
                "forecast_3d_temp_mean_celsius": fc_tmean,
                "precip_14d_mm": precip_14d,
                "precip_accum_28d_mm": cell_meteo_data.get("precip_accum_28d_mm"),
                "precip_15_28d_mm": cell_meteo_data.get("precip_15_28d_mm"),
                "effective_precip_14d_mm": round(eff_rain_active, 2),
                "effective_precip_tail_mm": round(eff_rain_tail, 2),
                "precip_incubation_mm": precip_incubation_mm,
                "precip_recent_4d_mm": precip_recent_4d_mm,
                "rain_timing_status": rain_timing_status,
                "precip_last_3d_mm": cell_meteo_data.get("precip_last_3d_mm"),
                "precip_score": round(precip_pts, 1),
                "temp_score": round(temp_pts, 1),
                "soil_moisture_score": round(soil_pts, 1),
                "drought_factor": round(drought_factor, 2),
                "waterlog_factor": round(waterlog_factor, 2),
                "thermal_shock_adj": round(thermal_shock_adj, 1),
                "humidity_adj": round(humidity_adj, 1),
                "wind_penalty_pct": round(wind_penalty * 100.0, 1),
                "slope_score": round(slope_pts, 1),
                "aspect_score": round(aspect_pts, 1),
                "temp_mean_c": temp_mean,
                "temp_recent_3d_c": temp_recent_3d,
                "temp_min_recent_3d_c": temp_min_recent,
                "soil_temp_mean_c": soil_temp,
                "soil_temp_0_7cm_3d_c": st07_recent,
                "frost_days_7d": frost_days_species_7d,
                "hard_frost_28d": hard_frost_28d,
                "humidity_mean_7d_pct": humidity_7d,
                "water_balance_14d_mm": water_balance,
                "thermal_shock_celsius": thermal_shock,
                "thermal_shock_status": thermal_shock_status,
                "wind_max_14d_kmh": wind_max,
                "phenology_factor": phenology_factor,
                "season_status": season_status,
                "soil_ph": soil_ph,
                "effective_soil_ph": effective_soil_ph,
                "lithology_class": lithology_class or None,
                "lithology_desc": lithology_desc,
                "lithology_adj": round(litho_adj, 1),
                "snowfall_7d_cm": snowfall_7d,
                "snowfall_3d_fc_cm": snowfall_3d_fc,
                "snow_factor": round(snow_factor, 2),
                "radiation_mean_7d_mj": rad_7d,
                "solar_factor": solar_factor,
                "host_tree": dominant_tree
            }
        )
        if relative_mode:
            result["weather_score"] = weather_score
        return result

    def _soil_soc_score(self, soc_g_kg: float) -> float:
        """Puntúa la riqueza orgánica superficial (SoilGrids SOC 0-5 cm)."""
        rules = self.config.get("soil_soc_scoring") or {}
        max_pts = float(rules.get("max_points", 3.0))
        low = float(rules.get("low_g_kg", 12.0))
        opt_min = float(rules.get("optimal_min_g_kg", 30.0))
        opt_max = float(rules.get("optimal_max_g_kg", 95.0))
        if opt_min <= soc_g_kg <= opt_max:
            return max_pts
        if soc_g_kg < opt_min:
            ratio = max(0.25, min(1.0, (soc_g_kg - low * 0.5) / max(1.0, opt_min - low * 0.5)))
            return max_pts * ratio
        return max_pts * max(0.70, 1.0 - (soc_g_kg - opt_max) / 250.0)

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

    def _static_habitat_assessment(
        self,
        species_id: str,
        cell_static_data: Dict[str, Any],
        allow_unknown_host: bool,
    ) -> tuple[bool | None, str, float, str | None]:
        rules = self.species_matrix[species_id]
        habitat = rules["habitat"]
        edaphic = rules["edaphic"]
        dominant_tree = cell_static_data["dominant_species"]
        canopy_cover = cell_static_data["canopy_cover"]
        elevation = cell_static_data["elevation_m"]
        effective_ph = self._effective_soil_ph(cell_static_data)

        host_assessment, host_tier, host_multiplier = self._assess_host_detail(
            dominant_tree,
            habitat["preferred_forest_types"],
            habitat.get("host_tiers"),
        )
        if host_assessment is False or (
            host_assessment is None and not allow_unknown_host
        ):
            return host_assessment, host_tier, host_multiplier, (
                "Exclusión: Especie forestal no hospedadora para micorriza"
            )
        if canopy_cover < habitat.get("minimum_canopy_cover_percent", 20):
            return host_assessment, host_tier, host_multiplier, (
                "Exclusión: Cobertura de copa insuficiente"
            )

        alt_min, alt_max = habitat["altitude_range_meters"]
        if elevation < (alt_min - 150) or elevation > (alt_max + 150):
            return host_assessment, host_tier, host_multiplier, (
                "Exclusión: Fuera del rango bioclimático altitudinal"
            )

        ph_tol_min, ph_tol_max = edaphic["ph_tolerated"]
        if effective_ph < ph_tol_min or effective_ph > ph_tol_max:
            return host_assessment, host_tier, host_multiplier, (
                f"Exclusión: pH {effective_ph} incompatible "
                f"(Tolerado: {ph_tol_min}-{ph_tol_max})"
            )
        return host_assessment, host_tier, host_multiplier, None

    def _static_habitat_exclusion(
        self,
        species_id: str,
        cell_static_data: Dict[str, Any],
        allow_unknown_host: bool,
    ) -> tuple[bool | None, str | None]:
        host_assessment, _, _, exclusion_reason = self._static_habitat_assessment(
            species_id, cell_static_data, allow_unknown_host=allow_unknown_host
        )
        return host_assessment, exclusion_reason

    _HOST_ALIASES = {
        "Pinus sylvestris": ("pino albar", "pinus sylvestris"),
        "Pinus nigra": ("pino salgareno", "pino laricio", "pinus nigra"),
        "Pinus pinaster": ("pino pinaster", "pinus pinaster"),
        "Pinus pinea": ("pino pinonero", "pinus pinea"),
        "Pinus halepensis": ("pino carrasco", "pinus halepensis"),
        "Pinus uncinata": ("pino negro", "pinus uncinata"),
        "Fagus sylvatica": ("hayedo", "haya", "fagus sylvatica"),
        "Quercus pyrenaica": ("melojar", "melojares", "quercus pyrenaica"),
        "Quercus petraea": ("roble albar", "quercus petraea", "quercus petrae"),
        "Quercus robur": ("roble carballo", "quercus robur"),
        "Quercus faginea": ("quejigar", "quejigo", "quercus faginea"),
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

    @classmethod
    def _assess_host_detail(
        cls,
        dominant_tree: str,
        preferred_forest_types: list[str],
        host_tiers: Optional[Dict[str, list[str]]] = None,
    ) -> tuple[bool | None, str, float]:
        normalized = cls._normalize_text(dominant_tree)
        tier_weights = {"primary": 1.0, "secondary": 0.86, "marginal": 0.70}

        ordered_preferred: list[tuple[str, str, float]] = []
        if host_tiers:
            for tier_name in ("primary", "secondary", "marginal"):
                for sp_name in host_tiers.get(tier_name) or ():
                    ordered_preferred.append(
                        (sp_name, tier_name, tier_weights[tier_name])
                    )
        seen_names = {item[0] for item in ordered_preferred}
        for sp_name in preferred_forest_types:
            if sp_name not in seen_names:
                ordered_preferred.append((sp_name, "primary", 1.0))

        for preferred, tier_name, mult in ordered_preferred:
            aliases = cls._HOST_ALIASES.get(preferred, (preferred.casefold(),))
            for alias in aliases:
                if cls._normalize_text(alias) in normalized:
                    return True, tier_name, mult

        if "coniferas y frondosas" in normalized:
            return None, "unknown", 0.80

        if (
            "mezclas de coniferas autoctonas" in normalized
            or "mezclas de coniferas con" in normalized
        ):
            accepts_conifer = any(
                "pinus" in cls._normalize_text(sp_name)
                for sp_name in preferred_forest_types
            )
            if accepts_conifer:
                return True, "mixed", 0.84
            return False, "incompatible", 0.0

        if "bosques mixtos de frondosas" in normalized:
            accepts_broadleaf = any(
                any(
                    genus in cls._normalize_text(sp_name)
                    for genus in ("quercus", "fagus", "castanea", "betula")
                )
                for sp_name in preferred_forest_types
            )
            if accepts_broadleaf:
                return True, "mixed", 0.84
            return False, "incompatible", 0.0

        for aliases in cls._HOST_ALIASES.values():
            for alias in aliases:
                if cls._normalize_text(alias) in normalized:
                    return False, "incompatible", 0.0
        return None, "unknown", 0.80

    @classmethod
    def _assess_host(
        cls, dominant_tree: str, preferred_forest_types: list[str]
    ) -> bool | None:
        assessment, _, _ = cls._assess_host_detail(
            dominant_tree, preferred_forest_types
        )
        return assessment

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
