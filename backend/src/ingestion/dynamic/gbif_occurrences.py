"""Importa registros GBIF de presencia con coordenadas y fecha acotadas."""
from datetime import datetime, timezone
import json

import duckdb
import requests

from backend.config.settings import DB_PATH, TARGET_REGION_BOUNDS

GBIF_SEARCH_URL = "https://api.gbif.org/v1/occurrence/search"
TARGET_TAXA = {
    5954958: "Boletus edulis",
    8733688: "Boletus aereus",
    5954949: "Boletus pinophilus",
    5954691: "Boletus reticulatus",
}
PAGE_SIZE = 300
MAX_COORDINATE_UNCERTAINTY_M = 1000


def _fetch_occurrences(session, taxon_key, current_year):
    min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS
    params = {
        "taxonKey": taxon_key,
        "hasCoordinate": "true",
        "hasGeospatialIssue": "false",
        "year": f"2000,{current_year}",
        "coordinate_uncertainty_in_meters": f"0,{MAX_COORDINATE_UNCERTAINTY_M}",
        "decimalLongitude": f"{min_lon},{max_lon}",
        "decimalLatitude": f"{min_lat},{max_lat}",
        "limit": PAGE_SIZE,
    }
    results = []
    offset = 0
    expected_count = None
    while expected_count is None or offset < expected_count:
        page_params = {**params, "offset": offset}
        response = session.get(GBIF_SEARCH_URL, params=page_params, timeout=90)
        response.raise_for_status()
        payload = response.json()
        if expected_count is None:
            expected_count = payload["count"]
        page = payload["results"]
        if not page and offset < expected_count:
            raise RuntimeError(
                f"GBIF devolvió una página vacía antes de completar el taxón {taxon_key}."
            )
        results.extend(page)
        offset += len(page)
    if len(results) != expected_count:
        raise RuntimeError(
            f"Descarga GBIF incompleta para {taxon_key}: "
            f"{len(results)} de {expected_count} registros."
        )
    return results, params


def ingest_gbif_occurrences():
    current_year = datetime.now(timezone.utc).year
    session = requests.Session()
    session.headers["User-Agent"] = "MapaSetero/1.0 (GBIF occurrence ingestion)"
    results_by_taxon = {}
    filters_by_taxon = {}
    for taxon_key in TARGET_TAXA:
        results_by_taxon[taxon_key], filters_by_taxon[taxon_key] = _fetch_occurrences(
            session, taxon_key, current_year
        )

    retrieved_at = datetime.now(timezone.utc).isoformat()
    occurrence_rows = []
    for taxon_key, results in results_by_taxon.items():
        for record in results:
            gbif_id = record.get("gbifID")
            latitude = record.get("decimalLatitude")
            longitude = record.get("decimalLongitude")
            uncertainty = record.get("coordinateUncertaintyInMeters")
            if gbif_id is None or latitude is None or longitude is None or uncertainty is None:
                raise RuntimeError(
                    f"GBIF devolvió un registro que no cumple los filtros del taxón {taxon_key}."
                )
            occurrence_rows.append((
                str(gbif_id),
                taxon_key,
                record.get("scientificName"),
                record.get("acceptedScientificName"),
                record.get("eventDate"),
                record.get("year"),
                float(latitude),
                float(longitude),
                float(uncertainty),
                record.get("basisOfRecord"),
                record.get("datasetKey"),
                record.get("license"),
                "presence_only",
                retrieved_at,
            ))

    with duckdb.connect(str(DB_PATH)) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS fungal_occurrences (
                gbif_id VARCHAR PRIMARY KEY,
                taxon_key BIGINT NOT NULL,
                scientific_name VARCHAR,
                accepted_scientific_name VARCHAR,
                event_date VARCHAR,
                event_year INTEGER,
                latitude DOUBLE NOT NULL,
                longitude DOUBLE NOT NULL,
                coordinate_uncertainty_m DOUBLE NOT NULL,
                basis_of_record VARCHAR,
                dataset_key VARCHAR,
                license VARCHAR,
                evidence_type VARCHAR NOT NULL,
                retrieved_at TIMESTAMP NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS fungal_occurrence_provenance (
                taxon_key BIGINT PRIMARY KEY,
                taxon_name VARCHAR NOT NULL,
                source_url VARCHAR NOT NULL,
                filters_json VARCHAR NOT NULL,
                record_count INTEGER NOT NULL,
                evidence_type VARCHAR NOT NULL,
                retrieved_at TIMESTAMP NOT NULL
            )
        """)
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(
                "DELETE FROM fungal_occurrences WHERE taxon_key IN (?, ?, ?, ?)",
                list(TARGET_TAXA),
            )
            con.execute(
                "DELETE FROM fungal_occurrence_provenance WHERE taxon_key IN (?, ?, ?, ?)",
                list(TARGET_TAXA),
            )
            con.executemany("""
                INSERT INTO fungal_occurrences VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, occurrence_rows)
            con.executemany("""
                INSERT INTO fungal_occurrence_provenance VALUES (
                    ?, ?, ?, ?, ?, ?, ?
                )
            """, [
                (
                    taxon_key,
                    TARGET_TAXA[taxon_key],
                    GBIF_SEARCH_URL,
                    json.dumps(filters_by_taxon[taxon_key], sort_keys=True),
                    len(results_by_taxon[taxon_key]),
                    "presence_only",
                    retrieved_at,
                )
                for taxon_key in TARGET_TAXA
            ])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

    for taxon_key, taxon_name in TARGET_TAXA.items():
        print(
            f"GBIF {taxon_name}: {len(results_by_taxon[taxon_key])} registros "
            "de presencia con coordenadas y fecha filtradas."
        )
    print(
        "Los registros solo aportan presencias, no abundancias ni ausencias; "
        "no permiten validar por sí solos una probabilidad de fructificación."
    )


if __name__ == "__main__":
    ingest_gbif_occurrences()
