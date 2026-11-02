"""Importa visitas de campo estandarizadas para futura validación micológica."""
import argparse
import csv
from datetime import date, datetime, timezone
import hashlib
import math
from pathlib import Path

import duckdb

from backend.config.settings import DB_PATH, TARGET_REGION_BOUNDS

TARGET_SPECIES = {
    "boletus_edulis": 5954958,
    "boletus_aereus": 8733688,
    "boletus_pinophilus": 5954949,
    "boletus_reticulatus": 5954691,
}
MAX_COORDINATE_UNCERTAINTY_M = 250
CSV_FIELDS = (
    "survey_id",
    "survey_date",
    "latitude",
    "longitude",
    "coordinate_uncertainty_m",
    "search_minutes",
    "observer_count",
    "protocol_id",
    *(f"detect_{species_id}" for species_id in TARGET_SPECIES),
)


def _read_surveys(csv_path: Path):
    with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("El CSV está vacío y no contiene cabecera.")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("El CSV contiene nombres de columna duplicados.")
        missing_fields = set(CSV_FIELDS) - set(reader.fieldnames)
        if missing_fields:
            raise ValueError(
                f"Faltan columnas requeridas en el CSV: {', '.join(sorted(missing_fields))}."
            )
        unexpected = set(reader.fieldnames) - set(CSV_FIELDS)
        if unexpected:
            raise ValueError(
                f"Columnas no reconocidas en el CSV: {', '.join(sorted(unexpected))}."
            )
        rows = []
        for line_number, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(
                    f"Fila {line_number}: número de valores distinto al de columnas."
                )
            rows.append(row)
    if not rows:
        raise ValueError("El CSV no contiene visitas de campo.")
    return rows


def _required_text(row, field, line_number):
    raw_value = row.get(field)
    if not isinstance(raw_value, str):
        raise ValueError(f"Fila {line_number}: '{field}' debe ser texto.")
    value = raw_value.strip()
    if not value:
        raise ValueError(f"Fila {line_number}: '{field}' no puede estar vacío.")
    return value


def _finite_number(row, field, line_number):
    raw = _required_text(row, field, line_number)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"Fila {line_number}: '{field}' debe ser numérico."
        ) from exc
    if not math.isfinite(value):
        raise ValueError(f"Fila {line_number}: '{field}' debe ser finito.")
    return value


def _parse_detection(row, field, line_number):
    value = _required_text(row, field, line_number).casefold()
    if value not in {"0", "1", "false", "true", "no", "sí", "si"}:
        raise ValueError(
            f"Fila {line_number}: '{field}' debe ser 0/1, false/true o no/sí."
        )
    return value in {"1", "true", "sí", "si"}


def _validated_rows(rows, current_date):
    min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS
    validated = []
    seen_ids = set()
    for line_number, row in enumerate(rows, start=2):
        survey_id = _required_text(row, "survey_id", line_number)
        if survey_id in seen_ids:
            raise ValueError(f"Fila {line_number}: survey_id duplicado '{survey_id}'.")
        seen_ids.add(survey_id)

        raw_survey_date = _required_text(row, "survey_date", line_number)
        try:
            surveyed_on = date.fromisoformat(raw_survey_date)
        except ValueError as exc:
            raise ValueError(
                f"Fila {line_number}: 'survey_date' debe usar YYYY-MM-DD."
            ) from exc
        if surveyed_on.isoformat() != raw_survey_date:
            raise ValueError(
                f"Fila {line_number}: 'survey_date' debe usar YYYY-MM-DD."
            )
        if surveyed_on > current_date:
            raise ValueError(f"Fila {line_number}: la visita no puede ser futura.")

        latitude = _finite_number(row, "latitude", line_number)
        longitude = _finite_number(row, "longitude", line_number)
        uncertainty = _finite_number(row, "coordinate_uncertainty_m", line_number)
        search_minutes = _finite_number(row, "search_minutes", line_number)
        observers = _finite_number(row, "observer_count", line_number)
        protocol_id = _required_text(row, "protocol_id", line_number)

        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError(f"Fila {line_number}: coordenadas fuera de rango.")
        if not min_lon <= longitude <= max_lon or not min_lat <= latitude <= max_lat:
            raise ValueError(
                f"Fila {line_number}: coordenadas fuera del área de estudio configurada."
            )
        if not 0 <= uncertainty <= MAX_COORDINATE_UNCERTAINTY_M:
            raise ValueError(
                f"Fila {line_number}: la incertidumbre debe estar entre 0 y "
                f"{MAX_COORDINATE_UNCERTAINTY_M} m."
            )
        if search_minutes <= 0 or not search_minutes.is_integer():
            raise ValueError(f"Fila {line_number}: 'search_minutes' debe ser entero positivo.")
        if observers <= 0 or not observers.is_integer():
            raise ValueError(f"Fila {line_number}: 'observer_count' debe ser entero positivo.")

        detections = {
            species_id: _parse_detection(
                row, f"detect_{species_id}", line_number
            )
            for species_id in TARGET_SPECIES
        }
        validated.append((
            survey_id,
            surveyed_on,
            latitude,
            longitude,
            uncertainty,
            int(search_minutes),
            int(observers),
            protocol_id,
            detections,
        ))
    return validated


def init_survey_schema(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS fungal_survey_imports (
            source_file_sha256 VARCHAR PRIMARY KEY,
            dataset_name VARCHAR NOT NULL,
            license VARCHAR NOT NULL,
            row_count INTEGER NOT NULL,
            imported_at TIMESTAMP NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS fungal_survey_visits (
            survey_id VARCHAR PRIMARY KEY,
            survey_date DATE NOT NULL,
            latitude DOUBLE NOT NULL,
            longitude DOUBLE NOT NULL,
            coordinate_uncertainty_m DOUBLE NOT NULL,
            search_minutes INTEGER NOT NULL,
            observer_count INTEGER NOT NULL,
            protocol_id VARCHAR NOT NULL,
            dataset_name VARCHAR NOT NULL,
            license VARCHAR NOT NULL,
            source_file_sha256 VARCHAR NOT NULL,
            imported_at TIMESTAMP NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS fungal_survey_detections (
            survey_id VARCHAR NOT NULL,
            taxon_key BIGINT NOT NULL,
            detected BOOLEAN NOT NULL,
            evidence_type VARCHAR NOT NULL,
            PRIMARY KEY (survey_id, taxon_key)
        )
    """)


def import_field_surveys(
    csv_path: Path,
    dataset_name: str,
    license_name: str,
    db_path: Path = DB_PATH,
    current_date: date | None = None,
):
    dataset_name = dataset_name.strip()
    license_name = license_name.strip()
    if not dataset_name or not license_name:
        raise ValueError("dataset_name y license son obligatorios.")
    csv_path = Path(csv_path)
    rows = _read_surveys(csv_path)
    validated = _validated_rows(rows, current_date or date.today())
    source_hash = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    imported_at = datetime.now(timezone.utc).replace(tzinfo=None)

    con = duckdb.connect(str(db_path))
    try:
        init_survey_schema(con)
        con.execute("BEGIN TRANSACTION")
        try:
            existing_import = con.execute("""
                SELECT dataset_name, license, row_count
                FROM fungal_survey_imports WHERE source_file_sha256 = ?
            """, [source_hash]).fetchone()
            import_values = (dataset_name, license_name, len(validated))
            if existing_import is not None and tuple(existing_import) != import_values:
                raise ValueError("El hash del CSV ya existe con metadatos incompatibles.")
            if existing_import is None:
                con.execute("""
                    INSERT INTO fungal_survey_imports VALUES (?, ?, ?, ?, ?)
                """, [
                    source_hash, dataset_name, license_name, len(validated), imported_at
                ])

            new_visits = 0
            for (
                survey_id, surveyed_on, latitude, longitude, uncertainty,
                search_minutes, observers, protocol_id, detections,
            ) in validated:
                existing = con.execute("""
                    SELECT survey_date, latitude, longitude, coordinate_uncertainty_m,
                           search_minutes, observer_count, protocol_id, dataset_name,
                           license
                    FROM fungal_survey_visits WHERE survey_id = ?
                """, [survey_id]).fetchone()
                visit_values = (
                    surveyed_on, latitude, longitude, uncertainty,
                    search_minutes, observers, protocol_id, dataset_name,
                    license_name,
                )
                if existing is not None:
                    if tuple(existing) != visit_values:
                        raise ValueError(
                            f"survey_id '{survey_id}' ya existe con datos diferentes."
                        )
                    existing_detections = con.execute("""
                        SELECT taxon_key, detected
                        FROM fungal_survey_detections
                        WHERE survey_id = ?
                    """, [survey_id]).fetchall()
                    expected_detections = {
                        (TARGET_SPECIES[species_id], detected)
                        for species_id, detected in detections.items()
                    }
                    if set(existing_detections) != expected_detections:
                        raise ValueError(
                            f"survey_id '{survey_id}' tiene detecciones incompatibles."
                        )
                    continue

                con.execute("""
                    INSERT INTO fungal_survey_visits VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                """, [
                    survey_id, surveyed_on, latitude, longitude, uncertainty,
                    search_minutes, observers, protocol_id, dataset_name,
                    license_name, source_hash, imported_at,
                ])
                con.executemany("""
                    INSERT INTO fungal_survey_detections
                    VALUES (?, ?, ?, 'standardized_field_survey')
                """, [
                    (survey_id, TARGET_SPECIES[species_id], detected)
                    for species_id, detected in detections.items()
                ])
                new_visits += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        summary = con.execute("""
            SELECT d.taxon_key, COUNT(*) AS surveyed_visits,
                   COUNT(*) FILTER (WHERE d.detected) AS detections,
                   COUNT(*) FILTER (WHERE NOT d.detected) AS non_detections,
                   COUNT(DISTINCT v.survey_date) AS surveyed_dates,
                   COUNT(DISTINCT CAST(v.latitude AS VARCHAR) || ',' ||
                                 CAST(v.longitude AS VARCHAR)) AS unique_locations,
                   COUNT(DISTINCT v.protocol_id) AS protocols
            FROM fungal_survey_detections d
            JOIN fungal_survey_visits v USING (survey_id)
            GROUP BY d.taxon_key
            ORDER BY d.taxon_key
        """).fetchall()
    finally:
        con.close()

    return {
        "imported_rows": len(validated),
        "new_visits": new_visits,
        "source_sha256": source_hash,
        "taxa_summary": summary,
    }


def write_template(path: Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(CSV_FIELDS)


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, help="Crea una plantilla CSV vacía.")
    parser.add_argument("--csv", type=Path, help="Importa visitas desde CSV.")
    parser.add_argument("--dataset-name", help="Nombre del conjunto de datos.")
    parser.add_argument("--license", dest="license_name", help="Licencia de los datos.")
    args = parser.parse_args(argv)

    if args.template:
        if args.csv or args.dataset_name or args.license_name:
            parser.error("--template no se combina con opciones de importación.")
        write_template(args.template)
        print(f"Plantilla vacía creada: {args.template}")
        return
    if not args.csv or not args.dataset_name or not args.license_name:
        parser.error("--csv, --dataset-name y --license son obligatorios para importar.")

    result = import_field_surveys(
        args.csv, args.dataset_name, args.license_name
    )
    print(
        f"Visitas procesadas: {result['imported_rows']}; "
        f"nuevas visitas: {result['new_visits']}. "
        f"SHA-256 del CSV: {result['source_sha256']}"
    )
    for taxon_key, visits, detections, non_detections, dates, locations, protocols in result["taxa_summary"]:
        print(
            f"taxonKey {taxon_key}: {visits} visitas, {detections} detecciones, "
            f"{non_detections} no detecciones, {dates} fechas, {locations} ubicaciones, "
            f"{protocols} protocolos."
        )
    print(
        "Una no detección significa que no se encontró durante esa visita; "
        "no demuestra ausencia biológica."
    )


if __name__ == "__main__":
    main()
