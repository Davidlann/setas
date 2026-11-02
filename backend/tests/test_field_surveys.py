import csv
from datetime import date
from pathlib import Path
import tempfile
import unittest

import duckdb

from backend.src.ingestion.dynamic.field_surveys import (
    CSV_FIELDS,
    TARGET_SPECIES,
    import_field_surveys,
    write_template,
)


class FieldSurveyImportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.csv_path = self.root / "surveys.csv"
        self.database_path = self.root / "test.duckdb"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_csv(self, rows):
        with self.csv_path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def _valid_row(self):
        row = {
            "survey_id": "visit-001",
            "survey_date": "2025-10-12",
            "latitude": "40.4",
            "longitude": "-3.7",
            "coordinate_uncertainty_m": "12",
            "search_minutes": "45",
            "observer_count": "2",
            "protocol_id": "fixed-route-v1",
        }
        row.update({
            f"detect_{species_id}": "no"
            for species_id in TARGET_SPECIES
        })
        row["detect_boletus_edulis"] = "sí"
        return row

    def test_import_stores_detection_and_non_detections(self):
        self._write_csv([self._valid_row()])

        result = import_field_surveys(
            self.csv_path,
            "Local pilot",
            "CC-BY-4.0",
            db_path=self.database_path,
            current_date=date(2026, 11, 2),
        )

        self.assertEqual(result["imported_rows"], 1)
        self.assertEqual(result["new_visits"], 1)
        with duckdb.connect(str(self.database_path), read_only=True) as con:
            visits = con.execute(
                "SELECT COUNT(*) FROM fungal_survey_visits"
            ).fetchone()[0]
            detections = con.execute("""
                SELECT detected FROM fungal_survey_detections
                WHERE survey_id = 'visit-001' AND taxon_key = ?
            """, [TARGET_SPECIES["boletus_edulis"]]).fetchone()[0]
            non_detections = con.execute("""
                SELECT COUNT(*) FROM fungal_survey_detections
                WHERE survey_id = 'visit-001' AND detected = FALSE
            """).fetchone()[0]
        self.assertEqual(visits, 1)
        self.assertTrue(detections)
        self.assertEqual(non_detections, 3)

    def test_reimport_is_idempotent(self):
        self._write_csv([self._valid_row()])
        kwargs = {
            "dataset_name": "Local pilot",
            "license_name": "CC-BY-4.0",
            "db_path": self.database_path,
            "current_date": date(2026, 11, 2),
        }

        import_field_surveys(self.csv_path, **kwargs)
        import_field_surveys(self.csv_path, **kwargs)

        with duckdb.connect(str(self.database_path), read_only=True) as con:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM fungal_survey_visits").fetchone()[0],
                1,
            )
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM fungal_survey_detections").fetchone()[0],
                4,
            )

    def test_importing_an_append_only_csv_keeps_prior_visits(self):
        first = self._valid_row()
        self._write_csv([first])
        options = {
            "dataset_name": "Local pilot",
            "license_name": "CC-BY-4.0",
            "db_path": self.database_path,
            "current_date": date(2026, 11, 2),
        }
        import_field_surveys(self.csv_path, **options)

        second = self._valid_row()
        second["survey_id"] = "visit-002"
        second["survey_date"] = "2025-10-19"
        self._write_csv([first, second])
        result = import_field_surveys(self.csv_path, **options)

        self.assertEqual(result["new_visits"], 1)
        with duckdb.connect(str(self.database_path), read_only=True) as con:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM fungal_survey_visits").fetchone()[0],
                2,
            )
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM fungal_survey_imports").fetchone()[0],
                2,
            )

    def test_rejects_coordinate_uncertainty_above_limit(self):
        row = self._valid_row()
        row["coordinate_uncertainty_m"] = "251"
        self._write_csv([row])

        with self.assertRaisesRegex(ValueError, "incertidumbre"):
            import_field_surveys(
                self.csv_path,
                "Local pilot",
                "CC-BY-4.0",
                db_path=self.database_path,
                current_date=date(2026, 11, 2),
            )

    def test_rejects_future_dates_and_incomplete_taxon_checks(self):
        row = self._valid_row()
        row["survey_date"] = "2026-11-03"
        self._write_csv([row])
        with self.assertRaisesRegex(ValueError, "futura"):
            import_field_surveys(
                self.csv_path,
                "Local pilot",
                "CC-BY-4.0",
                db_path=self.database_path,
                current_date=date(2026, 11, 2),
            )

        row["survey_date"] = "2025-10-12"
        row["detect_boletus_aereus"] = ""
        self._write_csv([row])
        with self.assertRaisesRegex(ValueError, "detect_boletus_aereus"):
            import_field_surveys(
                self.csv_path,
                "Local pilot",
                "CC-BY-4.0",
                db_path=self.database_path,
                current_date=date(2026, 11, 2),
            )

    def test_conflicting_existing_id_does_not_partially_import_file(self):
        first = self._valid_row()
        self._write_csv([first])
        options = {
            "dataset_name": "Local pilot",
            "license_name": "CC-BY-4.0",
            "db_path": self.database_path,
            "current_date": date(2026, 11, 2),
        }
        import_field_surveys(self.csv_path, **options)

        first["survey_id"] = "new-visit"
        conflicting = self._valid_row()
        conflicting["latitude"] = "40.5"
        self._write_csv([first, conflicting])
        with self.assertRaisesRegex(ValueError, "ya existe con datos diferentes"):
            import_field_surveys(self.csv_path, **options)

        with duckdb.connect(str(self.database_path), read_only=True) as con:
            ids = con.execute(
                "SELECT survey_id FROM fungal_survey_visits ORDER BY survey_id"
            ).fetchall()
        self.assertEqual(ids, [("visit-001",)])

    def test_rejects_noncanonical_date_and_malformed_csv_row(self):
        row = self._valid_row()
        row["survey_date"] = "20251012"
        self._write_csv([row])
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            import_field_surveys(
                self.csv_path,
                "Local pilot",
                "CC-BY-4.0",
                db_path=self.database_path,
                current_date=date(2026, 11, 2),
            )

        with self.csv_path.open("a", encoding="utf-8", newline="") as output:
            output.write("too,few,columns\n")
        with self.assertRaisesRegex(ValueError, "número de valores"):
            import_field_surveys(
                self.csv_path,
                "Local pilot",
                "CC-BY-4.0",
                db_path=self.database_path,
                current_date=date(2026, 11, 2),
            )

    def test_template_has_header_only(self):
        template_path = self.root / "template.csv"
        write_template(template_path)
        with template_path.open("r", encoding="utf-8-sig", newline="") as source:
            rows = list(csv.reader(source))
        self.assertEqual(rows, [list(CSV_FIELDS)])


if __name__ == "__main__":
    unittest.main()
