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
  [MDT de 200 m del IGN/SCNE](https://servicios.idee.es/wcs-inspire/mdt) y pH
  de 0–5 cm de [SoilGrids/ISRIC](https://files.isric.org/soilgrids/latest/data/phh2o/)
  (media y cuantiles 5–95), los muestrea en los centros de celdas H3 forestales,
  y registra fuente, unidades, resolución, fecha y hashes SHA-256 en DuckDB.
  Las teselas originales se guardan bajo `backend/data/raw/environmental/`.
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
- La matriz ecológica y sus puntuaciones son reglas orientativas, sin validación frente
  a fructificaciones. El visor separa el hábitat potencial (hospedador, suelo
  y relieve) de la señal meteorológica reciente. Ambas escalas 0–100 son
  relativas, **no probabilidades** ni tasas de éxito. La meteorología no reduce
  la capa de hábitat; una señal meteorológica baja tampoco demuestra ausencia
  de fructificación. El GBIF disponible es escaso y sesgado: se muestra como
  contexto, no se usa para calibrar las puntuaciones.
- Los índices experimentales (hábitat potencial y meteorología reciente por
  separado) se exportan con
  `.\backend\venv\Scripts\python.exe -m backend.src.export_relative_suitability`.
  Usa el snapshot meteorológico más reciente y escribe los cuatro conjuntos
  experimentales en `frontend/public/data/experimental/`. Ejecuta el comando
  después de actualizar la ingesta meteorológica para refrescar el mapa. El
  exportador validado y el pipeline diario siguen bloqueados para datos
  `sourced_unvalidated`; los snapshots históricos continúan apartados en
  `backend/data/processed/unverified_exports/`.
- Los cuatro conjuntos experimentales aplican una máscara estática compartida:
  solo exportan hexágonos que no incumplen las exclusiones de hábitat de al
  menos una especie. Las formaciones forestales mixtas cuyo hospedador no se
  puede identificar se conservan como inciertas. El filtro no descarta celdas
  por lluvia o temperatura, que pueden cambiar con la actualización diaria, y
  no borra datos de la base DuckDB.
- La capa de hábitat potencial no se reduce por el tiempo meteorológico del
  día: representa condiciones estáticas orientativas. La capa de meteorología
  reciente muestra una señal separada que combina lluvia, temperatura y
  humedad, sin interpretarla como presencia o ausencia.
- Los snapshots nuevos guardan la serie diaria de precipitación de 14 días.
  La señal meteorológica pondera menos la lluvia del día anterior y más la
  caída varios días atrás, como hipótesis de retardo explícita y configurable
  en el motor. Este retardo es una suposición y no está calibrado localmente; los
  snapshots anteriores no contienen la serie y recurren al acumulado simple
  hasta que se actualice la meteorología.
- La meteorología experimental se puede actualizar, aunque todavía no se pueda
  ejecutar el pipeline de scores validados, con
  `.\backend\venv\Scripts\python.exe -m backend.src.ingestion.dynamic.meteo_snapshots`.
  Después, vuelve a ejecutar el exportador experimental. La consulta usa una
  malla de 0,1° e interpolación IDW-4; esto suaviza la asignación entre nodos,
  pero no aporta precisión meteorológica real a escala de hexágono.
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

Abre `http://localhost:3000`. La interfaz muestra el índice ambiental
experimental cuando existe el conjunto generado e indica que no es una
probabilidad ni una predicción validada. En «Todas las especies», el mapa
colorea cada celda según el mayor índice de los cuatro boletus; al seleccionar
una celda, el panel muestra cuatro indicadores por especie: hospedador/cubierta,
altitud, pH estimado y condiciones recientes. Estos indicadores son orientativos:
no confirman ni descartan fructificación. Los botones de guía abren consejos
de búsqueda e identificación de cada especie, con fotografías acreditadas.
Un índice 0 indica que
una o más reglas limitan esa celda, no que se haya comprobado la ausencia de
setas. La meteorología se marca como contexto regional incierto en cada
hexágono: no debe usarse para afirmar diferencias meteorológicas entre dos
celdas vecinas. El pH se estima con SoilGrids y no es una medición local.

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
5. Cuando termine correctamente, visita
   `https://TU_USUARIO.github.io/mapa-setero/`.
6. En Android, abre esa dirección con Chrome y selecciona **Instalar app** o
   **Añadir a pantalla de inicio**. En iPhone/iPad, ábrela con Safari, toca
   **Compartir** y elige **Añadir a pantalla de inicio**.

El workflow obtiene el snapshot meteorológico de Open-Meteo, regenera los cuatro
índices experimentales y publica el sitio cada día a las 07:00 UTC (08:00/09:00
en la España peninsular, según el horario de invierno/verano). También se ejecuta
al subir cambios a `main` y se puede lanzar manualmente desde GitHub Actions.
Los datos nuevos aparecen al abrir o recargar la app con conexión. El modo
instalable guarda la última versión visitada de la interfaz y los datos que ya
se consultaron; el mapa base y las actualizaciones necesitan conexión.

Si un día falla la descarga o el cálculo, GitHub Actions marca la ejecución como
fallida y no despliega los datos incompletos. Revisa el registro en la pestaña
`Actions`; el sitio conserva la última publicación correcta. Los índices siguen
siendo experimentales, no probabilidades ni confirmaciones de fructificación.
