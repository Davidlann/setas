/**
 * Mapa setero — Motor de micro-detalle bajo demanda (H3-9 ~10,5 ha y H3-10 ~1,5 ha).
 * Combina fragmentos estáticos MFE50 + SoilGrids pH + IGME con teselas DEM Terrarium (~15 m)
 * descargadas en tiempo real para el entorno exclusivo de la celda seleccionada.
 */
(function (global) {
  'use strict';

  const TERRARIUM_ZOOM = 13;
  const TILE_SIZE = 256;
  const tileCache = new Map();
  const pendingTiles = new Map();
  const chunkCache = new Map();
  const pendingChunks = new Map();

  let activeParentCell = null;
  let activeSpeciesDataset = null;
  let activeResolution = 9;
  let activeSubcells = [];
  let selectedSubH = null;
  let microHandlersBound = false;
  let currentSelectCallback = null;

  const ASPECT_DIRS_ES = ['N', 'NE', 'E', 'SE', 'S', 'SO', 'O', 'NO'];

  function normStr(s) {
    return String(s || '')
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .toLowerCase()
      .trim();
  }

  const HOST_TOKENS = {
    'pinus sylvestris': ['pino albar', 'pinus sylvestris', 'pino silvestre'],
    'pinus nigra': ['pino salgareno', 'pino laricio', 'pinus nigra'],
    'pinus pinaster': ['pino pinaster', 'pino resinero', 'pinus pinaster'],
    'pinus pinea': ['pino pinonero', 'pinus pinea'],
    'pinus halepensis': ['pino carrasco', 'pinus halepensis'],
    'pinus uncinata': ['pino negro', 'pinus uncinata'],
    'fagus sylvatica': ['hayedo', 'haya', 'fagus sylvatica'],
    'quercus pyrenaica': ['melojar', 'rebollo', 'quercus pyrenaica'],
    'quercus petraea': ['roble albar', 'quercus petraea', 'quercus petrae'],
    'quercus robur': ['roble carballo', 'quercus robur'],
    'quercus faginea': ['quejigar', 'quejigo', 'quercus faginea'],
    'quercus ilex': ['encinar', 'encina', 'dehesa', 'quercus ilex'],
    'quercus suber': ['alcornocal', 'alcornoque', 'quercus suber'],
    'castanea sativa': ['castanar', 'castano', 'castanea sativa'],
    'betula alba': ['abedular', 'abedul', 'betula alba']
  };

  function assessSubcellHost(treeName, preferredTypes) {
    const t = normStr(treeName);
    if (!t) return 'clear';
    const prefs = Array.isArray(preferredTypes) ? preferredTypes : [];
    for (const pref of prefs) {
      const pNorm = normStr(pref);
      const tokens = HOST_TOKENS[pNorm] || [pNorm];
      if (tokens.some(tok => t.includes(tok))) return 'compatible';
    }
    if (t.includes('coniferas y frondosas')) return 'unknown';
    if (t.includes('mezclas de coniferas autoctonas') || t.includes('mezclas de coniferas con')) {
      return prefs.some(p => normStr(p).includes('pinus')) ? 'compatible' : 'incompatible';
    }
    if (t.includes('bosques mixtos de frondosas')) {
      return prefs.some(p => {
        const pn = normStr(p);
        return pn.includes('quercus') || pn.includes('fagus') || pn.includes('castanea') || pn.includes('betula');
      }) ? 'compatible' : 'incompatible';
    }
    return 'incompatible';
  }

  function latLonToWorldPixel(lat, lon, z = TERRARIUM_ZOOM) {
    const n = Math.pow(2, z) * TILE_SIZE;
    const x = ((lon + 180) / 360) * n;
    const sinLat = Math.sin((lat * Math.PI) / 180);
    const y = (0.5 - Math.log((1 + sinLat) / (1 - sinLat)) / (4 * Math.PI)) * n;
    return [x, y];
  }

  function fetchTerrariumTile(z, tx, ty) {
    const key = `${z}/${tx}/${ty}`;
    if (tileCache.has(key)) {
      const val = tileCache.get(key);
      tileCache.delete(key);
      tileCache.set(key, val);
      return Promise.resolve(val);
    }
    if (pendingTiles.has(key)) return pendingTiles.get(key);

    const url = `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/${z}/${tx}/${ty}.png`;
    const p = new Promise(resolve => {
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.onload = () => {
        try {
          const canvas = document.createElement('canvas');
          canvas.width = TILE_SIZE;
          canvas.height = TILE_SIZE;
          const ctx = canvas.getContext('2d', { willReadFrequently: true });
          ctx.drawImage(img, 0, 0);
          const data = ctx.getImageData(0, 0, TILE_SIZE, TILE_SIZE).data;
          const elev = new Float32Array(TILE_SIZE * TILE_SIZE);
          for (let i = 0, j = 0; i < elev.length; i += 1, j += 4) {
            elev[i] = (data[j] * 256 + data[j + 1] + data[j + 2] / 256) - 32768;
          }
          if (tileCache.size >= 16) {
            const oldest = tileCache.keys().next().value;
            tileCache.delete(oldest);
          }
          tileCache.set(key, elev);
          resolve(elev);
        } catch {
          resolve(null);
        }
      };
      img.onerror = () => resolve(null);
      img.src = url;
    }).finally(() => pendingTiles.delete(key));

    pendingTiles.set(key, p);
    return p;
  }

  async function sampleTerrainForPoints(points) {
    if (!points.length) return points.map(() => null);
    const radius = 3; // ventana 7x7 (~105 m para TPI local y 3x3 Horn para pendiente/vertiente)
    const pixelMeta = points.map(pt => {
      const [wx, wy] = latLonToWorldPixel(pt.lat, pt.lon, TERRARIUM_ZOOM);
      return { wx: Math.floor(wx), wy: Math.floor(wy), lat: pt.lat };
    });

    const neededTiles = new Set();
    for (const pm of pixelMeta) {
      const txMin = Math.floor((pm.wx - radius) / TILE_SIZE);
      const txMax = Math.floor((pm.wx + radius) / TILE_SIZE);
      const tyMin = Math.floor((pm.wy - radius) / TILE_SIZE);
      const tyMax = Math.floor((pm.wy + radius) / TILE_SIZE);
      for (let tx = txMin; tx <= txMax; tx += 1) {
        for (let ty = tyMin; ty <= tyMax; ty += 1) {
          neededTiles.add(`${TERRARIUM_ZOOM}/${tx}/${ty}`);
        }
      }
    }

    const loadedTiles = new Map();
    await Promise.all([...neededTiles].map(async key => {
      const [z, tx, ty] = key.split('/').map(Number);
      const arr = await fetchTerrariumTile(z, tx, ty);
      if (arr) loadedTiles.set(key, arr);
    }));

    function getElevAtGlobal(gx, gy) {
      const tx = Math.floor(gx / TILE_SIZE);
      const ty = Math.floor(gy / TILE_SIZE);
      const arr = loadedTiles.get(`${TERRARIUM_ZOOM}/${tx}/${ty}`);
      if (!arr) return null;
      const lx = gx - tx * TILE_SIZE;
      const ly = gy - ty * TILE_SIZE;
      return arr[ly * TILE_SIZE + lx];
    }

    return pixelMeta.map(pm => {
      const win = [];
      let sum7 = 0;
      let count7 = 0;
      for (let dy = -radius; dy <= radius; dy += 1) {
        for (let dx = -radius; dx <= radius; dx += 1) {
          const e = getElevAtGlobal(pm.wx + dx, pm.wy + dy);
          if (e === null || !Number.isFinite(e)) continue;
          sum7 += e;
          count7 += 1;
          if (Math.abs(dx) <= 1 && Math.abs(dy) <= 1) {
            win[(dy + 1) * 3 + (dx + 1)] = e;
          }
        }
      }
      if (win.length < 9 || win.some(v => v === undefined) || count7 < 25) return null;

      const metersPerPx = (156543.03392 * Math.cos((pm.lat * Math.PI) / 180)) / Math.pow(2, TERRARIUM_ZOOM);
      // Horn 3x3 gradient (y crecer hacia el sur en web mercator)
      const dzDx = (win[2] + 2 * win[5] + win[8] - win[0] - 2 * win[3] - win[6]) / (8 * metersPerPx);
      const dzDy = (win[6] + 2 * win[7] + win[8] - win[0] - 2 * win[1] - win[2]) / (8 * metersPerPx);
      const slopeRad = Math.atan(Math.hypot(dzDx, dzDy));
      const slope = Math.round((slopeRad * 180) / Math.PI * 10) / 10;
      let asp = null;
      if (slope >= 0.8) {
        // Vector normal descendente apunta hacia (-dzDx en Este, +dzDy en Norte porque y va al Sur)
        const az = ((Math.atan2(-dzDx, dzDy) * 180) / Math.PI + 360) % 360;
        asp = ASPECT_DIRS_ES[Math.round(az / 45) % 8];
      }
      const centerElev = win[4];
      const localTpi = centerElev - sum7 / count7;
      return {
        elev: Math.round(centerElev),
        slope,
        asp,
        tpi: Math.round(localTpi * 10) / 10
      };
    });
  }

  function solarFactorApprox(lat, slopeDeg, aspectEs, dateStr) {
    if (!Number.isFinite(slopeDeg) || slopeDeg < 1 || !aspectEs) return 1.0;
    const azMap = { N: 0, NE: 45, E: 90, SE: 135, S: 180, SO: 225, SW: 225, O: 270, W: 270, NO: 315, NW: 315 };
    const azDeg = azMap[String(aspectEs).toUpperCase()];
    if (azDeg === undefined) return 1.0;
    let doy = 285;
    if (dateStr) {
      const parsed = new Date(`${String(dateStr).slice(0, 10)}T12:00:00Z`);
      if (!Number.isNaN(parsed.getTime())) {
        const start = new Date(Date.UTC(parsed.getUTCFullYear(), 0, 0));
        doy = Math.round((parsed - start) / 86400000);
      }
    }
    const decl = 23.44 * Math.sin((2 * Math.PI * (doy - 81)) / 365);
    const zen = Math.max(3, Math.min(85, Math.abs((lat || 40.9) - decl))) * (Math.PI / 180);
    const slp = Math.max(0, Math.min(60, slopeDeg)) * (Math.PI / 180);
    const az = azDeg * (Math.PI / 180);
    const cosZ = Math.cos(zen);
    const cosI = Math.cos(slp) * cosZ - Math.sin(slp) * Math.cos(az) * Math.sin(zen);
    const eff = 0.25 + 0.75 * (Math.max(0.05, cosI) / Math.max(0.15, cosZ));
    return Math.round(Math.max(0.55, Math.min(1.35, eff)) * 100) / 100;
  }

  async function fetchMicroChunk(h5) {
    if (chunkCache.has(h5)) return chunkCache.get(h5);
    if (pendingChunks.has(h5)) return pendingChunks.get(h5);
    const p = fetch(`./public/data/micro/${h5}.json`)
      .then(res => (res.ok ? res.json() : null))
      .catch(() => null)
      .then(data => {
        chunkCache.set(h5, data);
        return data;
      })
      .finally(() => pendingChunks.delete(h5));
    pendingChunks.set(h5, p);
    return p;
  }

  function lithoLabelEs(code) {
    if (code === 'S') return 'Silíceo';
    if (code === 'C') return 'Calizo';
    if (code === 'M') return 'Mixto';
    return 'Sin dato';
  }

  function scoreMicroSubcell(subMeta, parentCell, speciesDataset) {
    const profile = (speciesDataset && speciesDataset.species_profile) || {};
    const speciesId = (speciesDataset && speciesDataset.species_id) || '';
    const parentCs = Number(parentCell.cs) || 0;
    const parentWs = Number(parentCell.ws) || 0;
    const parentHs = Number(parentCell.hs) || 0;

    const minCc = Number(profile.minimum_canopy_cover_percent || 15);
    const hostStatus = assessSubcellHost(subMeta.tree, profile.preferred_forest_types);

    if (!subMeta.tree) {
      return {
        ...subMeta,
        microScore: 0,
        status: 'excluded',
        reliefTag: subMeta.tpi <= -2.5 ? 'Claro en vaguada' : 'Claro / sin bosque',
        reason: 'Sin masa arbolada en este sub-hexágono (claro, pista o matorral).'
      };
    }
    if (hostStatus === 'incompatible') {
      return {
        ...subMeta,
        microScore: 0,
        status: 'excluded',
        reliefTag: 'Árbol no compatible',
        reason: `${subMeta.tree}: arbolado distinto al que busca esta especie.`
      };
    }
    if (subMeta.cc < minCc) {
      return {
        ...subMeta,
        microScore: 0,
        status: 'excluded',
        reliefTag: 'Copa muy abierta',
        reason: `Cobertura baja (${subMeta.cc}%; mínimo ${minCc}%).`
      };
    }

    // Reconstruimos base desde el padre cuando el padre sí puntuaba; si el padre estaba excluido pero este rodal SÍ es compatible, estimamos con clima del padre
    let baseScore = parentCs > 0 ? parentCs : (parentWs > 0 ? Math.pow(62, 0.4) * Math.pow(parentWs, 0.6) : 25);

    const parentElev = Number(parentCell.elev) || subMeta.elev || 1000;
    const dElev = (subMeta.elev || parentElev) - parentElev;
    const tOpt = Array.isArray(profile.mean_temp_optimal_celsius) ? profile.mean_temp_optimal_celsius : [8, 16];
    const minLimit = Number.isFinite(Number(profile.min_temp_limit_celsius)) ? Number(profile.min_temp_limit_celsius) : 0;

    const microSt = (Number(parentCell.stemp) || Number(parentCell.t) || 10) - 0.0045 * dElev;
    const microTmin = (Number.isFinite(Number(parentCell.tmin3)) ? Number(parentCell.tmin3) : Number(parentCell.tmin || 4)) - 0.006 * dElev;

    let adj = 0;

    // 1. Ajuste por árbol desconocido vs confirmado y espesura
    if (hostStatus === 'unknown') adj -= 8;
    if (subMeta.cc >= 35 && subMeta.cc <= 75) adj += 3;
    else if (subMeta.cc < 25) adj -= 4;

    // 2. Ajuste micro-altitud y helada en cota alta
    if (dElev > 40 && microTmin < minLimit + 0.5) {
      adj -= Math.min(18, (dElev / 40) * 3.5);
    } else if (dElev < -40 && microTmin > minLimit && microSt < tOpt[0]) {
      adj += Math.min(5, Math.abs(dElev) / 35);
    }
    if (microSt < 4.5 && speciesId !== 'lactarius_deliciosus') {
      adj -= 10;
    }

    // 3. Radiación solar / vertiente real en micro-relieve (15 m)
    const isCold = microSt < tOpt[0] + 1.5;
    const isDryOrHot = (Number(parentCell.wb) || 0) < -10 || microSt > tOpt[1] - 1.5;
    if (isCold) {
      adj += (subMeta.solarFactor - 1.0) * 18;
    } else if (isDryOrHot) {
      adj += (1.0 - subMeta.solarFactor) * 18;
    } else {
      adj += (subMeta.solarFactor - 1.0) * 6;
    }

    // 4. Micro-topografía: vaguadas conservan humedad; crestas se secan y ventean; pero vaguadas profundas atrapan helada
    const tpi = Number(subMeta.tpi) || 0;
    let reliefTag = 'Ladera uniforme';
    if (tpi <= -2.5) {
      if (microTmin <= minLimit + 0.5) {
        adj -= 6;
        reliefTag = 'Vaguada (hielo frío)';
      } else {
        adj += 5;
        reliefTag = 'Vaguada húmeda';
      }
    } else if (tpi >= 3.0) {
      adj -= 4.5;
      reliefTag = 'Loma / cresta expuesta';
    } else if (subMeta.solarFactor >= 1.08) {
      reliefTag = 'Ladera en solana';
    } else if (subMeta.solarFactor <= 0.88) {
      reliefTag = 'Ladera en umbría';
    }

    // 5. Pendiente transitable y retención
    const slope = Number(subMeta.slope) || 0;
    if (slope > 35) adj -= Math.min(20, (slope - 35) * 1.4);
    else if (slope > 26) adj -= (slope - 26) * 0.6;
    else if (slope >= 3 && slope <= 18) adj += 1.5;

    // 6. pH + litología micro-local
    const phTol = Array.isArray(profile.ph_tolerated) ? profile.ph_tolerated : [4.2, 7.5];
    const subPh = Number.isFinite(subMeta.ph) ? subMeta.ph : Number(parentCell.ph);
    if (Number.isFinite(subPh)) {
      if (subPh < phTol[0] || subPh > phTol[1] + 0.3) adj -= 12;
      else if (speciesId !== 'lactarius_deliciosus' && subMeta.lithCode === 'C') adj -= 4;
      else if (speciesId !== 'lactarius_deliciosus' && subMeta.lithCode === 'S') adj += 2;
    }

    const finalScore = Math.max(5, Math.min(98, Math.round((baseScore + adj) * 10) / 10));
    const reasons = [];
    if (reliefTag.includes('Vaguada húmeda')) reasons.push('vaguada que retiene humedad');
    else if (reliefTag.includes('Loma')) reasons.push('cresta más expuesta al viento');
    if (isCold && subMeta.solarFactor >= 1.06) reasons.push(`vertiente ${subMeta.asp || 'S'} soleada con frío`);
    else if (isCold && subMeta.solarFactor <= 0.88) reasons.push(`vertiente ${subMeta.asp || 'N'} fría con poca insolación`);
    if (slope > 28) reasons.push(`pendiente fuerte (${slope.toFixed(0)}°)`);
    if (parentHs === 0 && hostStatus === 'compatible') reasons.push('rodal compatible encontrado dentro de una celda heterogénea');

    return {
      ...subMeta,
      microScore: finalScore,
      status: finalScore >= 50 ? 'favorable' : 'intermediate',
      reliefTag,
      reason: reasons.length
        ? reasons.join(' · ')
        : `${subMeta.tree} (${subMeta.cc}%) en ${reliefTag.toLowerCase()}.`
    };
  }

  function microColor(score, status) {
    if (status === 'excluded' || score <= 0) return [100, 116, 139, 115];
    if (score >= 70) return [21, 128, 61, 215];
    if (score >= 50) return [16, 185, 129, 200];
    if (score >= 30) return [245, 158, 11, 190];
    return [220, 38, 38, 165];
  }

  function ensureMicroLayers(map) {
    if (!map || !map.getStyle()) return;
    if (!map.getSource('micro-h3')) {
      map.addSource('micro-h3', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] }
      });
      map.addLayer({
        id: 'micro-h3-fill',
        type: 'fill',
        source: 'micro-h3',
        paint: {
          'fill-color': ['rgb', ['get', '_r'], ['get', '_g'], ['get', '_b']],
          'fill-opacity': ['/', ['get', '_a'], 255]
        }
      });
      map.addLayer({
        id: 'micro-h3-line',
        type: 'line',
        source: 'micro-h3',
        paint: {
          'line-color': 'rgba(255,255,255,0.85)',
          'line-width': 1.4
        }
      });
      map.addLayer({
        id: 'micro-h3-label',
        type: 'symbol',
        source: 'micro-h3',
        layout: {
          'text-field': ['get', '_label'],
          'text-font': ['Open Sans Bold', 'Arial Unicode MS Bold'],
          'text-size': 11,
          'text-allow-overlap': true
        },
        paint: {
          'text-color': '#18100a',
          'text-halo-color': 'rgba(255,255,255,0.95)',
          'text-halo-width': 1.6
        }
      });
    }

    if (!microHandlersBound) {
      microHandlersBound = true;
      map.on('click', 'micro-h3-fill', e => {
        if (!e.features || !e.features.length) return;
        const props = e.features[0].properties;
        const match = activeSubcells.find(s => s.h === props.h) || props;
        selectMicroSubcell(map, match);
      });
      map.on('mouseenter', 'micro-h3-fill', () => { map.getCanvas().style.cursor = 'pointer'; });
      map.on('mouseleave', 'micro-h3-fill', () => { map.getCanvas().style.cursor = ''; });
    }
  }

  function renderMicroFeatures(map) {
    ensureMicroLayers(map);
    const features = activeSubcells.map(sc => {
      const boundary = global.h3.cellToBoundary(sc.h);
      const coords = boundary.map(([la, lo]) => [lo, la]);
      coords.push(coords[0]);
      const col = microColor(sc.microScore, sc.status);
      const aspLabel = sc.asp ? ` · ${sc.asp}` : '';
      return {
        type: 'Feature',
        properties: {
          ...sc,
          _r: col[0],
          _g: col[1],
          _b: col[2],
          _a: col[3],
          _label: sc.microScore > 0 ? `${Math.round(sc.microScore)}${aspLabel}` : '—'
        },
        geometry: { type: 'Polygon', coordinates: [coords] }
      };
    });
    const src = map.getSource('micro-h3');
    if (src) src.setData({ type: 'FeatureCollection', features });
    if (map.getLayer('micro-h3-line')) {
      map.setPaintProperty('micro-h3-line', 'line-color', [
        'case', ['==', ['get', 'h'], selectedSubH || ''], '#18181b', 'rgba(255,255,255,0.85)'
      ]);
      map.setPaintProperty('micro-h3-line', 'line-width', [
        'case', ['==', ['get', 'h'], selectedSubH || ''], 3.2, 1.3
      ]);
    }
  }

  function selectMicroSubcell(map, subcell) {
    if (!subcell) return;
    selectedSubH = subcell.h;
    renderMicroFeatures(map);
    if (typeof currentSelectCallback === 'function') {
      currentSelectCallback(subcell);
    }
  }

  async function openMicroDetail(map, parentCell, speciesDataset, resolution = 9, onSelectSubcell = null) {
    if (!map || !parentCell || !parentCell.h || !global.h3) return [];
    activeParentCell = parentCell;
    activeSpeciesDataset = speciesDataset;
    activeResolution = resolution === 10 ? 10 : 9;
    currentSelectCallback = onSelectSubcell;

    const h9Children = [...global.h3.cellToChildren(parentCell.h, 9)].sort();
    const neededH5 = new Set(h9Children.map(h9 => global.h3.cellToParent(h9, 5)));
    const chunksByH5 = new Map();
    await Promise.all([...neededH5].map(async h5 => {
      chunksByH5.set(h5, await fetchMicroChunk(h5));
    }));

    const h9MetaMap = new Map();
    for (const h9 of h9Children) {
      const h5 = global.h3.cellToParent(h9, 5);
      const chunk = chunksByH5.get(h5);
      const rec = chunk && chunk.c ? chunk.c[h9] : null;
      if (rec) {
        h9MetaMap.set(h9, {
          tree: (chunk.trees && chunk.trees[rec[0]]) || '',
          cc: Number(rec[1]) || 0,
          ph: rec[2] !== null && rec[2] !== undefined ? Number(rec[2]) / 10 : Number(parentCell.ph),
          lithCode: rec[3] || 'U'
        });
      } else {
        h9MetaMap.set(h9, {
          tree: parentCell.tree || '',
          cc: Number(parentCell.cc) || 0,
          ph: Number(parentCell.ph),
          lithCode: parentCell.lith === 'siliceous' ? 'S' : parentCell.lith === 'calcareous' ? 'C' : 'U'
        });
      }
    }

    const subIds = activeResolution === 10
      ? h9Children.flatMap(h9 => [...global.h3.cellToChildren(h9, 10)])
      : h9Children;

    const rawPoints = subIds.map(id => {
      const [lat, lon] = global.h3.cellToLatLng(id);
      const parentH9 = activeResolution === 10 ? global.h3.cellToParent(id, 9) : id;
      return { h: id, parentH9, lat, lon };
    });

    const terrainResults = await sampleTerrainForPoints(rawPoints);
    const snapDate = speciesDataset && speciesDataset.date ? speciesDataset.date : null;

    activeSubcells = rawPoints.map((pt, idx) => {
      const stMeta = h9MetaMap.get(pt.parentH9) || { tree: '', cc: 0, ph: Number(parentCell.ph), lithCode: 'U' };
      const ter = terrainResults[idx];
      const elev = ter ? ter.elev : Math.round(Number(parentCell.elev) || 1000);
      const slope = ter ? ter.slope : Math.round((Number(parentCell.slope) || 8) * 10) / 10;
      const asp = ter ? ter.asp : (parentCell.asp || null);
      const tpi = ter ? ter.tpi : 0;
      const solarFactor = solarFactorApprox(pt.lat, slope, asp, snapDate);
      return scoreMicroSubcell({
        h: pt.h,
        parentH9: pt.parentH9,
        lat: pt.lat,
        lon: pt.lon,
        tree: stMeta.tree,
        cc: stMeta.cc,
        ph: stMeta.ph,
        lithCode: stMeta.lithCode,
        lithLabel: lithoLabelEs(stMeta.lithCode),
        elev,
        slope,
        asp,
        tpi,
        solarFactor,
        demSource: ter ? 'Terrarium ~15 m' : 'MDT 200 m (fallback)'
      }, parentCell, speciesDataset);
    });

    const sortedBest = [...activeSubcells].sort((a, b) => b.microScore - a.microScore);
    selectedSubH = sortedBest[0] ? sortedBest[0].h : null;

    renderMicroFeatures(map);
    if (map.getLayer('h3-fill')) {
      map.setPaintProperty('h3-fill', 'fill-opacity', 0.22);
    }
    map.flyTo({
      center: [Number(parentCell.lon), Number(parentCell.lat)],
      zoom: activeResolution === 10 ? 15.1 : 14.2,
      speed: 1.2
    });
    if (sortedBest[0] && typeof currentSelectCallback === 'function') {
      currentSelectCallback(sortedBest[0]);
    }
    return activeSubcells;
  }

  function closeMicroDetail(map) {
    activeParentCell = null;
    activeSubcells = [];
    selectedSubH = null;
    if (map && map.getSource('micro-h3')) {
      map.getSource('micro-h3').setData({ type: 'FeatureCollection', features: [] });
    }
    if (map && map.getLayer('h3-fill')) {
      map.setPaintProperty('h3-fill', 'fill-opacity', ['*', ['/', ['get', '_a'], 255], 0.86]);
    }
  }

  global.MapaSeteroMicro = {
    openMicroDetail,
    closeMicroDetail,
    getState: () => ({
      active: Boolean(activeParentCell),
      resolution: activeResolution,
      parentCell: activeParentCell,
      subcells: activeSubcells,
      selectedSubH
    }),
    solarFactorApprox,
    scoreMicroSubcell
  };
})(window);
