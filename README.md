# Mapa setero - visor micológico

Visor experimental para explorar información forestal y ambiental en Madrid,
Segovia, Soria y Guadalajara. **No es un pronóstico validado de fructificación
ni una guía de recolección.**

## Estado y calidad de los datos

- El MFE50 de MITECO aporta información de formaciones forestales; las rutinas
  actuales no garantizan cubrir todas las masas ni asignar correctamente todos
  los rodales a hexágonos.
- Open-Meteo aporta variables meteorológicas de modelos regionales. El pipeline
  consulta una malla de 0,1° y combina los cuatro nodos más cercanos mediante
  ponderación inversa a la distancia. Esto evita los bloques abruptos de la
  asignación al nodo más cercano, pero no crea observaciones a escala de H3:
  sus hexágonos (~0,74 km²) siguen siendo mucho menores que la resolución real
  del modelo. La lluvia y la humedad del suelo son estimaciones, no mediciones
  locales; la humedad corresponde al suelo superficial modelado, no a humedad
  del aire ni a una sonda en el bosque.
- La ingesta ambiental puede ejecutarse con
  `.\backend\venv\Scripts\python.exe -m backend.src.ingestion.static.environmental_layers`.
  Obtiene el
  [MDT de 200 m del IGN/SCNE](https://servicios.idee.es/wcs-inspire/mdt)
  (elevación, pendiente, orientación e índice de posición topográfica `tpi_m`
  en vecindario 5×5) y pH de 0–5 cm de
  [SoilGrids/ISRIC](https://files.isric.org/soilgrids/latest/data/phh2o/)
  (media y cuantiles 5–95), además de arena, limo y arcilla de 5–15 cm y
  carbono orgánico superficial (`soc` 0–5 cm). Estas proporciones se almacenan
  como porcentaje en masa o g/kg y se muestrean en los centros de celdas H3
  forestales; la fuente, unidades, resolución, fecha y hashes SHA-256 quedan
  registrados en DuckDB.
  Las teselas originales se guardan bajo `backend/data/raw/environmental/`.
- La textura y el carbono orgánico de SoilGrids contribuyen a los 35 puntos
  edáficos junto con el pH en humus amortiguado según tipo de masa forestal:
  la matriz aplica una regla general y gradual que penaliza extremos de arena
  (>70–90%), limo (>60–80%) y arcilla (>40–60%) y valora la riqueza orgánica.
  Es una heurística experimental, no una preferencia validada de cada especie.
  Si falta alguna fracción, no se penaliza su ausencia y se mantiene la
  puntuación anterior basada en pH. La textura no permite inferir por sí sola
  si el sustrato es silíceo o calizo. Para obtenerla, vuelve a ejecutar la
  ingesta ambiental y después el exportador experimental.
- Estas capas tienen procedencia, pero se marcan `sourced_unvalidated`, nunca
  `verified`: la resolución de 200/250 m es mucho más gruesa que algunas
  variables locales, y las entradas no han sido contrastadas con mediciones
  de campo. Derivar pendiente y orientación no aumenta la resolución del MDT.
- Los registros de fructificación de GBIF se pueden importar con
  `.\backend\venv\Scripts\python.exe -m backend.src.ingestion.dynamic.gbif_occurrences`
  desde la
  [API de ocurrencias](https://api.gbif.org/v1/occurrence/search). Se filtran
  por taxón, fecha (2000–año actual), coordenadas, ausencia de incidencias
  geográficas y error de coordenadas de hasta 1 km. Se conserva la licencia y
  la clave del conjunto de datos. Son presencias oportunistas, sin ausencias ni
  esfuerzo de muestreo; su error espacial puede superar el tamaño de una celda
  H3 y no sirven por sí solas para entrenar o validar una probabilidad.
- De forma opcional, si en el futuro hay campañas de muestreo disponibles, se
  pueden importar para validar el modelo con
  `.\backend\venv\Scripts\python.exe -m backend.src.ingestion.dynamic.field_surveys --template .\field_surveys.csv`.
  Cada visita debe indicar fecha, coordenadas, esfuerzo y detecciones/no
  detecciones. Este paso **no es necesario** para generar el índice orientativo.
- La matriz ecológica y sus puntuaciones son reglas orientativas, sin validación
  frente a fructificaciones. El visor separa el hábitat potencial (hospedador,
  pH, textura y relieve) de la señal meteorológica reciente; la capa «Índice
  combinado» usa la media geométrica de ambos scores por especie. Las tres
  escalas 0–100 son relativas, **no probabilidades** ni tasas de éxito. La
  meteorología no reduce la capa de hábitat; una señal meteorológica baja
  tampoco demuestra ausencia de fructificación. El GBIF disponible es escaso y
  sesgado: se muestra como contexto, no se usa para calibrar las puntuaciones.
- Los índices experimentales (hábitat potencial, meteorología reciente e índice
  combinado) se exportan en formato dividido v2 con
  `.\backend\venv\Scripts\python.exe -m backend.src.export_relative_suitability`.
  Genera un fichero compartido `frontend/public/data/experimental/cells_base.json`
  (geometría, suelo, meteorología diaria 28d + previsión 3d, litología y capa
  legal) junto con cinco ficheros ligeros por especie (*Boletus edulis*,
  *B. aereus*, *B. pinophilus*, *B. reticulatus* y *Lactarius deliciosus* /
  níscalo), incluyendo la tendencia diaria `dcs` frente a la fecha previa (`prev_date`).
  El exportador validado y el pipeline diario siguen bloqueados para datos
  `sourced_unvalidated`; los snapshots históricos continúan apartados en
  `backend/data/processed/unverified_exports/`.
- La capa de litología IGME 1:1M y normativa básica (Parques Nacionales OAPN y
  cotos regulados IDECyL / Micocyl en Castilla y León) se actualiza con
  `.\backend\venv\Scripts\python.exe -m backend.src.ingestion.static.lithology_legal`,
  generando `frontend/public/data/legal_zones.geojson` y avisos en cada celda.
- De forma opcional, si configuras la variable de entorno o secreto de GitHub
  `AEMET_API_KEY` (clave gratuita de AEMET OpenData), `meteo_openmeteo.py` y
  `aemet_opendata.py` calibran el sesgo regional de precipitación y mínimas con
  observaciones reales de estaciones cercanas; sin clave, el pipeline continúa
  sin errores usando Open-Meteo e interpolación altitudinal.
- El motor (`backend/src/scoring/engine.py`) incorpora litología silícea/caliza,
  penalización y alerta 72 h de nevadas (`snowfall_7d_cm`, `snow_risk`) y
  radiación solar topográfica por vertiente (`_topographic_solar_factor`).
- El micro-detalle bajo demanda divide cada celda H3-8 (~74 ha) en **7 sub-zonas
  H3-9 (~10,5 ha)** o **49 micro-zonas H3-10 (~1,5 ha)** combinando rodales
  precalculados MFE50, pH e IGME (`backend.src.processing.build_micro_index` ->
  `frontend/public/data/micro/`) con muestreo bajo demanda de relieve a ~15 m
  (AWS Terrarium z13 DEM) para vertiente, pendiente, TPI local y factor solar.
  Esta herramienta orienta la exploración por ladera dentro de la celda y no
  sustituye la cartografía oficial sobre el terreno.
- Los cinco conjuntos experimentales aplican una máscara estática compartida:
  solo exportan hexágonos que no incumplen las exclusiones de hábitat de al
  menos una especie. Las formaciones forestales mixtas cuyo hospedador no se
  puede identificar se conservan como inciertas. El filtro no descarta celdas
  por lluvia o temperatura, que pueden cambiar con la actualización diaria, y
  no borra datos de la base DuckDB.
- La capa de hábitat potencial no se reduce por el tiempo meteorológico del
  día: representa condiciones estáticas orientativas. La capa de meteorología
  reciente muestra una señal separada (acoplada multiplicativamente bajo la ley
  del mínimo) que combina lluvia eficaz ponderada por tiempo térmico sobre
  28 días, arrastre de sequía previa, riesgo de encharcamiento frío, balance
  hídrico ($P - ET_0$), temperatura del suelo superficial y radicular (0–28 cm),
  choque térmico fisiológico, heladas, viento desecante y dormancia invernal de
  caducifolias, además de mostrar una previsión orientativa a 72 horas y una
  fase estimada de brotada, sin interpretarla como presencia o ausencia confirmada.
- Los snapshots meteorológicos guardan la serie diaria de 28 días y previsión
  de 3 días para los nodos 0,1° que cubren las celdas forestales, con control
  adaptativo de cuotas Open-Meteo (`X-RateLimit-*`) y poda automática de
  snapshots antiguos más compactación atómica de DuckDB para mantener el
  repositorio ligero.
- Para *Boletus edulis*, *Boletus pinophilus* y *Lactarius deliciosus* (níscalo), el motor detecta el choque térmico
  (caída brusca de temperatura dentro del rango activo tras lluvia suficiente y
  sin helada) como estímulo fisiológico favorable para la inducción de primordios,
  penalizando en cambio el frío paralizante o las olas de calor. *Lactarius deliciosus*
  amplía además la cobertura a pinares silíceos y calizos (*Pinus sylvestris*,
  *P. pinaster*, *P. nigra*, *P. uncinata*, *P. pinea*, *P. halepensis*) y tolera
  ambientes más fríos de finales de otoño e inicio de invierno. Para especies
  termófilas (*Boletus aereus* y *Boletus reticulatus*), el frío prematuro o las
  heladas detienen la señal meteorológica.
- Rachas de viento superiores al umbral de cada especie (40–50 km/h) penalizan
  la señal por riesgo de desecación y aborto de carpóforos en cumbres y laderas.
- La meteorología experimental se puede actualizar, aunque todavía no se pueda
  ejecutar el pipeline de scores validados, con
  `.\backend\venv\Scripts\python.exe -m backend.src.ingestion.dynamic.meteo_snapshots`.
  Después, vuelve a ejecutar el exportador experimental. La consulta cubre la
  malla de 0,1° adyacente a masas forestales e interpola mediante IDW-4 con ajuste
  altitudinal y reintentos adaptativos ante cuotas; esto suaviza la asignación
  entre nodos, pero no aporta precisión meteorológica real a escala de hexágono
  microclimático.
- En formaciones MFE50 reconocibles se interpretan nombres comunes de
  hospedadores. Cuando una formación mixta no permite identificar la especie
  arbórea, el índice la penaliza por incertidumbre en vez de afirmar que el
  hongo no puede estar allí. La carta muestra esta incertidumbre por celda.

La resolución H3 8 tiene un área media aproximada de 0,74 km² (74 ha) y una
arista media de unos 531 m. La malla completa aún no representa fielmente los
límites forestales y administrativos.

## Ejecución local

Desde la raíz del proyecto, en PowerShell:

```powershell
.\backend\venv\Scripts\python.exe -m uvicorn backend.src.api.main:app --reload --port 8000
```

La API estará en `http://localhost:8000/docs`. Para abrir el visor estático,
sirve `frontend` en otra terminal:

```powershell
Set-Location .\frontend
python -m http.server 3000
```

Abre `http://localhost:3000`. La interfaz muestra índices ambientales
experimentales, no probabilidades ni predicciones validadas. Incluye selector de
mapa base (`Mapa`, `Topo IGN` MTN y `Satélite PNOA` del IGN), capa opcional de
Parques Nacionales y cotos Micocyl, filtros por fase de brotada y distancia GPS,
gráfica de evolución 28 días + previsión 72 h, inspección bajo demanda en
micro-detalle (7 sub-zonas H3-9 o 49 micro-zonas H3-10), guardado de **zonas
habituales** y un **diario local de setales** (100 % privado en `localStorage` del
navegador, con exportación/importación JSON y descarga GPX). Puedes marcar una o
varias especies; al seleccionar una celda se muestran sus indicadores de
hábitat, altitud, pH y meteorología. La capa inicial «Índice combinado» calcula
la media geométrica de los índices experimentales de hábitat y tiempo para cada
especie; si se eligen varias, muestra el mayor índice combinado entre ellas.
Esto evita que el índice combinado copie sistemáticamente el valor meteorológico
cuando el hábitat tiene una puntuación superior. Sigue siendo una orientación
experimental, no una probabilidad ni un modelo validado de fructificación. Las
capas de hábitat y meteorología también se pueden consultar por separado.

El filtro de lluvia limita las celdas según la precipitación estimada acumulada
en los últimos 14 días. La meteorología es regional y no representa diferencias
fiables entre hexágonos vecinos; la humedad y el pH tampoco son mediciones
locales. Las mínimas recientes bajo el umbral térmico experimental de cada
especie reducen el índice meteorológico; ese umbral se incluye en el perfil
exportado de cada especie. Las guías ofrecen temporadas generales,
pero el mes aún no se usa como factor numérico: la época cambia según región,
altitud, bosque y año. Una puntuación baja o cero no demuestra que no haya setas.
Las guías incluyen consejos prácticos de hábitat y búsqueda e imágenes
acreditadas; no sustituyen la identificación experta.

## Pipeline

El procesador MFE50, el pipeline diario y los exportadores están en `backend/src`.
El índice orientativo puede ejecutarse con las fuentes disponibles. No lo
presentes como probabilidad ni como fructificación confirmada; el cálculo
validado seguirá deshabilitado hasta evaluar y calibrar sus resultados con un
conjunto independiente.

Dependencias Python: `backend/requirements.txt`.

## Instalar en el móvil y publicar actualizaciones diarias

El frontend es una PWA (aplicación web instalable). Se puede añadir a la pantalla
de inicio desde el navegador; no se distribuye como APK ni se publica en las
tiendas. Necesita HTTPS para instalarse, por eso se recomienda GitHub Pages.

### Publicación gratuita con GitHub Pages

1. Crea en GitHub un repositorio **público** vacío llamado, por ejemplo,
   `mapa-setero`. GitHub Pages gratuito requiere que el repositorio sea público.
2. Desde PowerShell, en la carpeta raíz del proyecto, inicializa y sube el
   código. Sustituye `TU_USUARIO` por tu usuario de GitHub:

   ```powershell
   git init
   git add .
   git commit -m "Publica Mapa setero"
   git branch -M main
   git remote add origin https://github.com/TU_USUARIO/mapa-setero.git
   git push -u origin main
   ```

   El `.gitignore` evita subir el entorno virtual y las capas brutas descargadas.
   La base `backend/data/processed/mycologist_spatial.duckdb` sí debe estar en el
   repositorio: el proceso diario la necesita para recalcular los datos.
3. En GitHub, abre `Settings > Pages`, elige `GitHub Actions` como fuente de
   publicación y guarda.
4. En `Actions`, permite las acciones si GitHub muestra una confirmación. El
   primer despliegue comienza al subir `main`; también puedes ejecutarlo con
   `Actions > Actualizar y publicar Mapa setero > Run workflow`.
   Opcionalmente, si añades tu clave gratuita de AEMET OpenData como secreto
   `AEMET_API_KEY` en `Settings > Secrets and variables > Actions`, las
   ejecuciones automáticas calibrarán el sesgo regional de precipitación y mínimas.
5. Cuando termine correctamente, visita
   `https://TU_USUARIO.github.io/mapa-setero/`.
6. En Android, abre esa dirección con Chrome y selecciona **Instalar app** o
   **Añadir a pantalla de inicio**. En iPhone/iPad, ábrela con Safari, toca
   **Compartir** y elige **Añadir a pantalla de inicio**.

El workflow obtiene el snapshot meteorológico de Open-Meteo, regenera los cinco
índices experimentales y publica el sitio dos veces al día (05:30 y 13:30 UTC).
También se ejecuta al subir cambios a `main` y se puede lanzar manualmente desde
GitHub Actions. Si falla la descarga o el cálculo diario, el workflow restaura
los cinco conjuntos de datos válidos anteriores, publica la interfaz con aviso y
marca el job como fallido para alertar del error en el ETL.
