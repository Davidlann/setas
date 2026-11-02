"""
Módulo de indexación espacial Uber H3 con soporte dual para h3-py v3 y v4.
Maneja la generación de índices, polígonos GeoJSON y mallas espaciales para España.
"""
from typing import List, Dict, Any, Tuple
import h3

# Detección de versión de h3-py
IS_H3_V4 = hasattr(h3, 'latlng_to_cell')

def lat_lng_to_h3(lat: float, lng: float, resolution: int = 8) -> str:
    """Convierte coordenadas WGS84 (Lat, Lng) al índice H3 correspondiente."""
    if IS_H3_V4:
        return h3.latlng_to_cell(lat, lng, resolution)
    return h3.geo_to_h3(lat, lng, resolution)

def h3_to_lat_lng(h3_index: str) -> Tuple[float, float]:
    """Obtiene el centroide (lat, lng) de un índice H3."""
    if IS_H3_V4:
        return h3.cell_to_latlng(h3_index)
    return h3.h3_to_geo(h3_index)

def h3_to_geojson_polygon(h3_index: str) -> Dict[str, Any]:
    """Convierte un índice H3 en una Feature GeoJSON Polygon con coordenadas [lon, lat]."""
    if IS_H3_V4:
        # En v4, cell_to_boundary devuelve tuplas (lat, lng)
        boundary = [[lng, lat] for lat, lng in h3.cell_to_boundary(h3_index)]
        # Cerrar el polígono si no está cerrado
        if boundary and boundary[0] != boundary[-1]:
            boundary.append(boundary[0])
    else:
        boundary = h3.h3_to_geo_boundary(h3_index, geo_json=True)
        if boundary and boundary[0] != boundary[-1]:
            boundary.append(boundary[0])

    return {
        "type": "Feature",
        "properties": {
            "h3_index": h3_index
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [boundary]
        }
    }

def get_h3_grid_for_bbox(min_lon: float, min_lat: float, max_lon: float, max_lat: float, resolution: int = 8) -> List[str]:
    """
    Genera el conjunto de hexágonos H3 que cubren un Bounding Box rectangular.
    """
    if IS_H3_V4:
        # En v4 se usa GeoPolygon o un geojson dict compatible
        geo_poly = {
            "type": "Polygon",
            "coordinates": [[
                [min_lon, min_lat],
                [max_lon, min_lat],
                [max_lon, max_lat],
                [min_lon, max_lat],
                [min_lon, min_lat]
            ]]
        }
        return list(h3.polygon_to_cells(geo_poly, resolution))
    else:
        polygon_geojson = {
            "type": "Polygon",
            "coordinates": [[
                [min_lon, min_lat],
                [max_lon, min_lat],
                [max_lon, max_lat],
                [min_lon, max_lat],
                [min_lon, min_lat]
            ]]
        }
        return list(h3.polyfill(polygon_geojson, resolution, geo_json_conformant=True))

def get_ring_neighbors(h3_index: str, ring_distance: int = 1) -> List[str]:
    """Obtiene los hexágonos en un radio k para análisis zonal."""
    if IS_H3_V4:
        return list(h3.grid_disk(h3_index, ring_distance))
    return list(h3.k_ring(h3_index, ring_distance))
