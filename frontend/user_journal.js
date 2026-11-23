/**
 * Mapa setero — Módulo local de zonas habituales, diario privado de setales,
 * exportación GPX y gráfica SVG de 28 días + previsión 72 h.
 * Todos los datos del usuario permanecen exclusivamente en localStorage del navegador.
 */
(function (global) {
  'use strict';

  const KEY_FAV_ZONES = 'mapa_setero_favorite_zones_v1';
  const KEY_OUTINGS = 'mapa_setero_outings_v1';

  const OUTCOME_LABELS = {
    abundant: 'Cesta buena / Abundantes',
    buttons: 'Botones jóvenes',
    mature: 'Crecidos / Cola',
    none: 'Nada (0 ejemplares)'
  };

  function safeLoad(key) {
    try {
      const raw = global.localStorage ? global.localStorage.getItem(key) : null;
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }

  function safeSave(key, list) {
    try {
      if (global.localStorage) {
        global.localStorage.setItem(key, JSON.stringify(list));
      }
      return true;
    } catch {
      return false;
    }
  }

  function getFavoriteZones() {
    return safeLoad(KEY_FAV_ZONES);
  }

  function saveFavoriteZone(zone) {
    if (!zone || !zone.h) return getFavoriteZones();
    const list = getFavoriteZones();
    const id = String(zone.id || zone.h);
    const entry = {
      id,
      h: String(zone.h),
      name: String(zone.name || 'Zona guardada').trim().slice(0, 64),
      lat: Number(zone.lat),
      lon: Number(zone.lon),
      savedAt: zone.savedAt || new Date().toISOString()
    };
    const idx = list.findIndex(z => z.id === id || z.h === entry.h);
    if (idx >= 0) list[idx] = entry;
    else list.unshift(entry);
    safeSave(KEY_FAV_ZONES, list);
    return list;
  }

  function removeFavoriteZone(idOrH) {
    const target = String(idOrH);
    const next = getFavoriteZones().filter(z => z.id !== target && z.h !== target);
    safeSave(KEY_FAV_ZONES, next);
    return next;
  }

  function isCellFavorite(h) {
    if (!h) return false;
    return getFavoriteZones().some(z => z.h === h);
  }

  function getOutings() {
    return safeLoad(KEY_OUTINGS);
  }

  function addOuting(outing) {
    if (!outing) return getOutings();
    const list = getOutings();
    const id = String(outing.id || `out-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`);
    const entry = {
      id,
      date: String(outing.date || new Date().toISOString().slice(0, 10)).slice(0, 10),
      name: String(outing.name || 'Setal').trim().slice(0, 72),
      h: outing.h ? String(outing.h) : '',
      subH: outing.subH ? String(outing.subH) : '',
      lat: Number(outing.lat),
      lon: Number(outing.lon),
      speciesId: String(outing.speciesId || 'boletus_edulis'),
      outcome: OUTCOME_LABELS[outing.outcome] ? outing.outcome : 'none',
      note: String(outing.note || '').trim().slice(0, 260),
      snapshot: outing.snapshot || null
    };
    const idx = list.findIndex(o => o.id === id);
    if (idx >= 0) list[idx] = entry;
    else list.unshift(entry);
    list.sort((a, b) => String(b.date).localeCompare(String(a.date)));
    safeSave(KEY_OUTINGS, list);
    return list;
  }

  function removeOuting(id) {
    const next = getOutings().filter(o => o.id !== String(id));
    safeSave(KEY_OUTINGS, next);
    return next;
  }

  function buildOutingsGeoJSON(outings) {
    const rows = Array.isArray(outings) ? outings : getOutings();
    return {
      type: 'FeatureCollection',
      features: rows
        .filter(o => Number.isFinite(o.lat) && Number.isFinite(o.lon))
        .map(o => ({
          type: 'Feature',
          properties: {
            id: o.id,
            date: o.date,
            name: o.name,
            h: o.h,
            speciesId: o.speciesId,
            outcome: o.outcome,
            outcomeLabel: OUTCOME_LABELS[o.outcome] || o.outcome,
            note: o.note || '',
            lat: o.lat,
            lon: o.lon
          },
          geometry: { type: 'Point', coordinates: [o.lon, o.lat] }
        }))
    };
  }

  function exportBackupJson() {
    const payload = {
      version: 1,
      app: 'Mapa setero',
      exportedAt: new Date().toISOString(),
      favoriteZones: getFavoriteZones(),
      outings: getOutings()
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `mapa-setero-diario-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    return payload;
  }

  function importBackupJson(jsonText) {
    const parsed = JSON.parse(jsonText);
    if (!parsed || typeof parsed !== 'object') {
      throw new Error('Formato JSON no válido.');
    }
    const existingFavs = getFavoriteZones();
    const favByKey = new Map(existingFavs.map(z => [z.h || z.id, z]));
    let addedFavs = 0;
    if (Array.isArray(parsed.favoriteZones)) {
      for (const z of parsed.favoriteZones) {
        if (!z || !z.h || !Number.isFinite(Number(z.lat)) || !Number.isFinite(Number(z.lon))) continue;
        if (!favByKey.has(z.h)) {
          favByKey.set(z.h, {
            id: String(z.id || z.h),
            h: String(z.h),
            name: String(z.name || 'Zona guardada').slice(0, 64),
            lat: Number(z.lat),
            lon: Number(z.lon),
            savedAt: z.savedAt || new Date().toISOString()
          });
          addedFavs += 1;
        }
      }
      safeSave(KEY_FAV_ZONES, [...favByKey.values()]);
    }

    const existingOut = getOutings();
    const outById = new Map(existingOut.map(o => [o.id, o]));
    let addedOutings = 0;
    if (Array.isArray(parsed.outings)) {
      for (const o of parsed.outings) {
        if (!o || !Number.isFinite(Number(o.lat)) || !Number.isFinite(Number(o.lon))) continue;
        const oid = String(o.id || `${o.date}-${o.lat}-${o.lon}`);
        if (!outById.has(oid)) {
          outById.set(oid, {
            id: oid,
            date: String(o.date || new Date().toISOString().slice(0, 10)).slice(0, 10),
            name: String(o.name || 'Setal').slice(0, 72),
            h: String(o.h || ''),
            subH: String(o.subH || ''),
            lat: Number(o.lat),
            lon: Number(o.lon),
            speciesId: String(o.speciesId || 'boletus_edulis'),
            outcome: OUTCOME_LABELS[o.outcome] ? o.outcome : 'none',
            note: String(o.note || '').slice(0, 260),
            snapshot: o.snapshot || null
          });
          addedOutings += 1;
        }
      }
      const mergedOut = [...outById.values()].sort((a, b) => String(b.date).localeCompare(String(a.date)));
      safeSave(KEY_OUTINGS, mergedOut);
    }
    return { addedFavs, addedOutings };
  }

  function escapeXml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&apos;');
  }

  function downloadGpxWaypoint({ name, lat, lon, desc }) {
    if (!Number.isFinite(Number(lat)) || !Number.isFinite(Number(lon))) return;
    const latNum = Number(lat).toFixed(6);
    const lonNum = Number(lon).toFixed(6);
    const gpx = `<?xml version="1.0" encoding="UTF-8"?>\n` +
      `<gpx version="1.1" creator="MapaSetero" xmlns="http://www.topografix.com/GPX/1/1">\n` +
      `  <wpt lat="${latNum}" lon="${lonNum}">\n` +
      `    <name>${escapeXml(name || 'Punto setero')}</name>\n` +
      `    <desc>${escapeXml(desc || 'Coordenada exportada desde Mapa setero')}</desc>\n` +
      `  </wpt>\n</gpx>\n`;
    const blob = new Blob([gpx], { type: 'application/gpx+xml;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const slug = String(name || 'setal').toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 32);
    a.download = `${slug || 'setal'}.gpx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function svgEl(tag, attrs = {}) {
    const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs)) {
      el.setAttribute(k, String(v));
    }
    return el;
  }

  function renderMeteoChart28d(container, cell, dataset) {
    if (!container) return;
    container.replaceChildren();

    const p28 = Array.isArray(cell && cell.p28d) ? cell.p28d.map(Number) : [];
    const st28 = Array.isArray(cell && cell.st28d) ? cell.st28d.map(Number) : [];
    const tn28 = Array.isArray(cell && cell.tn28d) ? cell.tn28d.map(Number) : [];
    const fcRain = Array.isArray(cell && cell.fcr3d) ? cell.fcr3d.map(Number) : [];
    const fcTmin = Array.isArray(cell && cell.fctn3d) ? cell.fctn3d.map(Number) : [];

    if (p28.length < 7 && st28.length < 7) {
      const msg = document.createElement('p');
      msg.className = 'species-comparison-note';
      msg.textContent = 'Sin serie diaria de 28 días disponible en este snapshot.';
      container.append(msg);
      return;
    }

    const nHist = p28.length || Math.max(st28.length, tn28.length);
    const nFc = Math.min(3, Math.max(fcRain.length, fcTmin.length));
    const totalDays = nHist + nFc;

    const W = 320;
    const H = 122;
    const padL = 22;
    const padR = 22;
    const padT = 12;
    const padB = 18;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const stepX = plotW / Math.max(1, totalDays);

    const allRain = [...p28, ...fcRain].filter(Number.isFinite);
    const maxRain = Math.max(20, ...allRain, 1);

    const allTemps = [...st28, ...tn28, ...fcTmin, 0, 12].filter(Number.isFinite);
    const minTemp = Math.min(-4, Math.floor(Math.min(...allTemps) - 1));
    const maxTemp = Math.max(18, Math.ceil(Math.max(...allTemps) + 1));
    const yForTemp = t => padT + plotH - ((t - minTemp) / Math.max(1, maxTemp - minTemp)) * plotH;
    const xForIdx = i => padL + (i + 0.5) * stepX;

    const svg = svgEl('svg', {
      viewBox: `0 0 ${W} ${H}`,
      class: 'meteo-chart-svg',
      role: 'img',
      'aria-label': 'Lluvia y temperatura de suelo y mínima de los últimos 28 días y previsión 3 días'
    });

    // Fondo de zona futura (+3d)
    if (nFc > 0) {
      const xSplit = padL + nHist * stepX;
      svg.appendChild(svgEl('rect', {
        x: xSplit.toFixed(1),
        y: padT,
        width: (W - padR - xSplit).toFixed(1),
        height: plotH,
        fill: 'rgba(14, 165, 233, 0.08)',
        rx: 2
      }));
      svg.appendChild(svgEl('line', {
        x1: xSplit.toFixed(1),
        x2: xSplit.toFixed(1),
        y1: padT,
        y2: padT + plotH,
        stroke: 'rgba(100, 116, 139, 0.35)',
        'stroke-dasharray': '2,2',
        'stroke-width': 1
      }));
    }

    // Línea base de 0 °C
    if (minTemp <= 0 && maxTemp >= 0) {
      const y0 = yForTemp(0);
      svg.appendChild(svgEl('line', {
        x1: padL,
        x2: W - padR,
        y1: y0.toFixed(1),
        y2: y0.toFixed(1),
        stroke: 'rgba(59, 130, 246, 0.45)',
        'stroke-dasharray': '3,2',
        'stroke-width': 0.9
      }));
      const lbl0 = svgEl('text', {
        x: W - padR + 3,
        y: (y0 + 3).toFixed(1),
        fill: '#2563eb',
        'font-size': '8',
        'font-weight': '700'
      });
      lbl0.textContent = '0°';
      svg.appendChild(lbl0);
    }

    // Barras de lluvia (28d históricos + 3d previsión)
    const barW = Math.max(3, stepX * 0.68);
    p28.forEach((rain, idx) => {
      if (!Number.isFinite(rain) || rain <= 0) return;
      const barH = Math.min(plotH * 0.85, (rain / maxRain) * plotH * 0.85);
      const x = xForIdx(idx) - barW / 2;
      const y = padT + plotH - barH;
      svg.appendChild(svgEl('rect', {
        x: x.toFixed(1),
        y: y.toFixed(1),
        width: barW.toFixed(1),
        height: Math.max(1.5, barH).toFixed(1),
        fill: idx >= 14 && idx <= 23 ? 'rgba(2, 132, 199, 0.72)' : 'rgba(56, 189, 248, 0.55)',
        rx: 1
      }));
    });
    fcRain.slice(0, nFc).forEach((rain, idx) => {
      if (!Number.isFinite(rain) || rain <= 0) return;
      const barH = Math.min(plotH * 0.85, (rain / maxRain) * plotH * 0.85);
      const x = xForIdx(nHist + idx) - barW / 2;
      const y = padT + plotH - barH;
      svg.appendChild(svgEl('rect', {
        x: x.toFixed(1),
        y: y.toFixed(1),
        width: barW.toFixed(1),
        height: Math.max(1.5, barH).toFixed(1),
        fill: 'rgba(6, 182, 212, 0.35)',
        stroke: '#0891b2',
        'stroke-width': 0.7,
        'stroke-dasharray': '1.5,1',
        rx: 1
      }));
    });

    // Curva Tmín histórica
    const tminPts = tn28
      .map((t, i) => Number.isFinite(t) ? `${xForIdx(i).toFixed(1)},${yForTemp(t).toFixed(1)}` : null)
      .filter(Boolean);
    if (tminPts.length >= 2) {
      svg.appendChild(svgEl('polyline', {
        fill: 'none',
        stroke: '#4f46e5',
        'stroke-width': 1.4,
        'stroke-linejoin': 'round',
        'stroke-linecap': 'round',
        points: tminPts.join(' ')
      }));
    }
    // Prolongación previsión 3d Tmín
    if (fcTmin.length && tn28.length) {
      const lastTn = tn28[tn28.length - 1];
      const fcPts = [];
      if (Number.isFinite(lastTn)) {
        fcPts.push(`${xForIdx(nHist - 1).toFixed(1)},${yForTemp(lastTn).toFixed(1)}`);
      }
      fcTmin.slice(0, nFc).forEach((t, i) => {
        if (Number.isFinite(t)) fcPts.push(`${xForIdx(nHist + i).toFixed(1)},${yForTemp(t).toFixed(1)}`);
      });
      if (fcPts.length >= 2) {
        svg.appendChild(svgEl('polyline', {
          fill: 'none',
          stroke: '#4f46e5',
          'stroke-width': 1.4,
          'stroke-dasharray': '2.5,2',
          points: fcPts.join(' ')
        }));
      }
    }

    // Curva temperatura suelo radicular
    const stPts = st28
      .map((t, i) => Number.isFinite(t) ? `${xForIdx(i).toFixed(1)},${yForTemp(t).toFixed(1)}` : null)
      .filter(Boolean);
    if (stPts.length >= 2) {
      svg.appendChild(svgEl('polyline', {
        fill: 'none',
        stroke: '#d97706',
        'stroke-width': 1.9,
        'stroke-linejoin': 'round',
        'stroke-linecap': 'round',
        points: stPts.join(' ')
      }));
    }

    // Etiquetas eje X y escala izquierda de lluvia máx
    const maxRainLbl = svgEl('text', { x: 2, y: padT + 7, fill: '#0284c7', 'font-size': '7.5', 'font-weight': '700' });
    maxRainLbl.textContent = `${Math.round(maxRain)}mm`;
    svg.appendChild(maxRainLbl);

    const ticks = [
      { idx: 0, text: '-28d' },
      { idx: Math.floor(nHist / 2), text: '-14d' },
      { idx: nHist - 1, text: 'Hoy' },
    ];
    if (nFc > 0) ticks.push({ idx: totalDays - 1, text: `+${nFc}d` });
    ticks.forEach(({ idx, text }) => {
      const lbl = svgEl('text', {
        x: xForIdx(idx).toFixed(1),
        y: H - 4,
        fill: '#64748b',
        'font-size': '8',
        'text-anchor': idx === 0 ? 'start' : idx === totalDays - 1 ? 'end' : 'middle',
        'font-weight': idx >= nHist - 1 ? '700' : '500'
      });
      lbl.textContent = text;
      svg.appendChild(lbl);
    });

    container.append(svg);

    // Leyenda compacta debajo del gráfico
    const legend = document.createElement('div');
    legend.className = 'meteo-chart-legend';
    const sum28 = p28.reduce((acc, v) => acc + (Number.isFinite(v) ? v : 0), 0);
    const sumFc = fcRain.reduce((acc, v) => acc + (Number.isFinite(v) ? v : 0), 0);
    const lastSt = st28.length ? st28[st28.length - 1] : cell.stemp;
    const minFc = fcTmin.length ? Math.min(...fcTmin.filter(Number.isFinite)) : cell.fc_tn;
    const items = [
      { color: '#0284c7', text: `Lluvia 28d: ${Math.round(sum28)} mm` },
      { color: '#d97706', text: `Suelo hoy: ${Number.isFinite(lastSt) ? lastSt.toFixed(1) : '—'} °C` },
      { color: '#4f46e5', text: `Mín. 3d / Próx.: ${Number.isFinite(Number(cell.tmin3)) ? Number(cell.tmin3).toFixed(1) : '—'}° / ${Number.isFinite(minFc) ? minFc.toFixed(1) : '—'}°` },
      { color: '#0891b2', text: `Próx. 3d: ${sumFc.toFixed(0)} mm` }
    ];
    items.forEach(item => {
      const span = document.createElement('span');
      span.className = 'meteo-chart-leg-item';
      const dot = document.createElement('i');
      dot.style.background = item.color;
      span.append(dot, document.createTextNode(item.text));
      legend.append(span);
    });
    container.append(legend);
  }

  global.MapaSeteroJournal = {
    OUTCOME_LABELS,
    getFavoriteZones,
    saveFavoriteZone,
    removeFavoriteZone,
    isCellFavorite,
    getOutings,
    addOuting,
    removeOuting,
    buildOutingsGeoJSON,
    exportBackupJson,
    importBackupJson,
    downloadGpxWaypoint,
    renderMeteoChart28d
  };
})(window);
