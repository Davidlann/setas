"""Motor experimental de puntuación micológica basado en reglas heurísticas."""
from typing import Dict, Any, Optional
import yaml
from pathlib import Path
import math
import unicodedata

class MushroomScoringEngine:
    PRECIPITATION_LAG_WEIGHTS = (
        0.20, 0.30, 0.45, 0.60, 0.75, 0.85, 0.95,
        1.00, 1.00, 0.90, 0.75, 0.55, 0.35, 0.20,
    )

    def __init__(self, matrix_path: Optional[str] = None):
        if matrix_path is None:
            config_dir = Path(__file__).resolve().parent.parent.parent / "config"
            matrix_path = config_dir / "species_matrix.yaml"

        with open(matrix_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
            self.species_matrix = self.config.get("species", {})

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
        aspect = cell_static_data.get("aspect_cardinal")
        if aspect is None and not allow_missing_aspect:
            raise ValueError("La orientación es obligatoria para el índice validado.")
        if aspect is not None and (
            not isinstance(aspect, str) or aspect.upper() not in {
            "N", "NE", "E", "SE", "S", "SW", "W", "NW"
            }
        ):
            raise ValueError("La orientación debe ser una dirección cardinal válida.")
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
        effective_precip = precip_14d
        if daily_precipitation is not None:
            weight_mean = sum(self.PRECIPITATION_LAG_WEIGHTS) / len(
                self.PRECIPITATION_LAG_WEIGHTS
            )
            effective_precip = sum(
                rain * weight
                for rain, weight in zip(
                    reversed(daily_precipitation),
                    self.PRECIPITATION_LAG_WEIGHTS,
                )
            ) / weight_mean

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
        t_opt_min, t_opt_max = meteo_rules["mean_temp_optimal_celsius"]
        if t_opt_min <= temp_mean <= t_opt_max:
            temp_pts = 15.0
        elif abs(temp_mean - t_opt_min) <= 4.0 or abs(temp_mean - t_opt_max) <= 4.0:
            temp_pts = 9.0
        else:
            temp_pts = 3.0

        # Penalización por heladas tempranas destructivas del carpóforo
        min_limit = meteo_rules.get("min_temp_limit_celsius", 0.0)
        if temp_min < min_limit:
            temp_pts *= 0.3 # Caída drástica por congelación

        # Factor Humedad del suelo (0 a 5 pts)
        min_soil_m = meteo_rules.get("soil_moisture_min_percent", 20.0)
        soil_pts = 5.0 if soil_moisture >= min_soil_m else (soil_moisture / min_soil_m) * 5.0

        score_meteo = precip_pts + temp_pts + soil_pts
        weather_score = min(100.0, round(score_meteo / 45.0 * 100.0, 1))

        # ==============================================================
        # 3. PONDERACIÓN EDAFOLÓGICA (Máximo 35 puntos)
        # ==============================================================
        ph_opt_min, ph_opt_max = edaphic["ph_optimal"]
        ph_tol_min, ph_tol_max = edaphic["ph_tolerated"]
        if ph_opt_min <= soil_ph <= ph_opt_max:
            score_edaphic = 35.0
        else:
            # Distancia al rango óptimo dentro del tolerado
            if soil_ph < ph_opt_min:
                dist = ph_opt_min - soil_ph
                max_d = ph_opt_min - ph_tol_min
            else:
                dist = soil_ph - ph_opt_max
                max_d = ph_tol_max - ph_opt_max

            ratio = max(0.0, 1.0 - (dist / max(0.1, max_d)))
            score_edaphic = 15.0 + ratio * 20.0

        # ==============================================================
        # 4. PONDERACIÓN TOPOGRÁFICA (Máximo 20 puntos)
        # ==============================================================
        topo_rules = rules["topography"]
        slope = cell_static_data["slope_deg"]
        aspect = aspect.upper() if aspect else None

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
            total_score *= 0.75
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
                "temp_mean_c": temp_mean,
                "soil_ph": soil_ph,
                "host_tree": dominant_tree
            }
        )
        if relative_mode:
            result["weather_score"] = weather_score
        return result

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
        soil_ph = cell_static_data["soil_ph"]

        host_assessment = self._assess_host(
            dominant_tree, habitat["preferred_forest_types"]
        )
        if host_assessment is False or (
            host_assessment is None and not allow_unknown_host
        ):
            return host_assessment, (
                "Exclusión: Especie forestal no hospedadora para micorriza"
            )
        if canopy_cover < habitat.get("minimum_canopy_cover_percent", 30):
            return host_assessment, "Exclusión: Cobertura de copa insuficiente"

        alt_min, alt_max = habitat["altitude_range_meters"]
        if elevation < (alt_min - 150) or elevation > (alt_max + 150):
            return host_assessment, (
                "Exclusión: Fuera del rango bioclimático altitudinal"
            )

        ph_tol_min, ph_tol_max = edaphic["ph_tolerated"]
        if soil_ph < ph_tol_min or soil_ph > ph_tol_max:
            return host_assessment, (
                f"Exclusión: pH {soil_ph} incompatible "
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
            "Pinus sylvestris": ("pino albar", "pinus sylvestris"),
            "Pinus nigra": ("pino salgareno", "pino salgareño", "pino laricio", "pinus nigra"),
            "Pinus pinaster": ("pino pinaster", "pinus pinaster"),
            "Pinus pinea": ("pino pinonero", "pinus pinea"),
            "Pinus halepensis": ("pino carrasco", "pinus halepensis"),
            "Pinus uncinata": ("pino negro", "pinus uncinata"),
            "Fagus sylvatica": ("hayedo", "haya", "fagus sylvatica"),
            "Quercus pyrenaica": ("melojar", "melojares", "quercus pyrenaica"),
            "Quercus petraea": ("roble albar", "quercus petraea"),
            "Quercus robur": ("roble carballo", "quercus robur"),
            "Quercus faginea": ("quejigar", "quejigo", "quercus faginea"),
            "Quercus ilex": ("encinar", "encina", "quercus ilex"),
            "Quercus suber": ("alcornocal", "alcornoque", "quercus suber"),
            "Castanea sativa": ("castanar", "castaño", "castanea sativa"),
            "Betula alba": ("abedular", "abedul", "betula alba"),
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
