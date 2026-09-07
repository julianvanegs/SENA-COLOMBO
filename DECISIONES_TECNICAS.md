# Documento de decisiones técnicas

**Proyecto:** Repositorio analítico de desempeño BYD
**Pipeline:** CSV (generación asistida por IA) → Python/pandas → PostgreSQL (Docker) → Power BI

---

## 1. Diseño del modelo analítico

### 1.1 Decisión: modelo relacional en Tercera Forma Normal (3FN)

El repositorio analítico se diseñó como un modelo de dos tablas con relación uno-a-muchos:

| Tabla | Rol | Granularidad |
|---|---|---|
| `empresas` | Dimensión | Una fila por empresa |
| `desempeno_anual` | Hechos | Una fila por empresa y por fecha |

**Por qué 3FN y no una tabla plana:**

1. **Elimina la redundancia del nombre de empresa.** En el CSV original, el texto de la empresa se repite en cada fila. Al extraerlo a una dimensión con `id_empresa`, la tabla de hechos guarda un entero en lugar de una cadena repetida, lo que reduce el tamaño y elimina la posibilidad de que la misma empresa aparezca escrita de dos formas distintas dentro de los hechos.

2. **Protege la integridad referencial.** La llave foránea `desempeno_anual.id_empresa → empresas.id_empresa` impide cargar métricas de una empresa que no existe en la dimensión. Es una garantía a nivel de base de datos, no una convención que dependa del script.

3. **Facilita la extensión a más competidores.** El análisis compara BYD contra Tesla. Agregar un tercer o cuarto competidor no requiere cambiar el esquema: solo se insertan filas nuevas en ambas tablas.

4. **Power BI consume esto de forma nativa.** Al importar dos tablas relacionadas, Power BI detecta la relación y permite filtrar todos los visuales por empresa desde un único segmentador, sin duplicar medidas.

### 1.2 Llave primaria compuesta

La tabla de hechos usa `PRIMARY KEY (id_empresa, fecha)`. Esta decisión hace que **la propia base de datos garantice la regla de negocio**: una empresa no puede tener dos registros de desempeño para la misma fecha. Aunque el script ya elimina duplicados en pandas, la llave compuesta actúa como segunda línea de defensa si el pipeline se ejecuta con datos corruptos.

### 1.3 Separación de columnas por tipo de dato

Las columnas se clasifican explícitamente en dos listas antes de la limpieza:

- **`columna_numericas`** — magnitudes absolutas (montos en millones de USD, unidades vendidas, año). Se redondean a entero porque los decimales no aportan precisión útil en escalas de millones.
- **`columnas_float`** — ratios y porcentajes expresados como decimal (`_dec`). Se conservan con 4 decimales porque una diferencia de 0.0001 en un margen neto sí es material.

Esta separación evita el error más común en pipelines de este tipo: aplicar redondeo a entero sobre un margen de 0.0512 y convertirlo en 0.

### 1.4 Convención de nombres

Todas las columnas se normalizan a `snake_case` en minúscula con sufijo de unidad:

- `_usd_mn` → millones de dólares
- `_dec` → decimal entre 0 y 1 (no porcentaje ya multiplicado)
- `pct_` → prefijo para participaciones

La unidad va en el nombre de la columna para que sea imposible confundir 16 (por ciento) con 0.16 (decimal) al construir medidas en Power BI.

---

## 2. Cumplimiento de las reglas mínimas

| Regla | Estado | Implementación |
|---|---|---|
| Sin nulos sin tratar en columnas clave | Cumple | Ver §3.2 y §3.3 |
| Duplicados eliminados con criterio explícito | Cumple | Ver §2.1 |
| Fechas en formato único | Cumple (corregido) | Ver §2.2 |
| Texto de agrupación normalizado | Cumple | Ver §2.3 |
| Mínimo dos métricas derivadas | Cumple | Ver §2.4 |

### 2.1 Criterio de duplicado (explícito, en dos niveles)

El pipeline aplica dos definiciones distintas de duplicado, en orden:

```python
# Nivel 1 — duplicado exacto: la fila completa se repite en el CSV origen.
# Causa típica: doble exportación o concatenación accidental de archivos.
df.drop_duplicates(inplace=True)

# Nivel 2 — duplicado de negocio: dos registros de la misma empresa
# para la misma fecha. Aunque las métricas difieran, solo puede existir
# un registro de desempeño por empresa-periodo. Se conserva el primero
# por orden de aparición en el archivo fuente.
df_desempeno.drop_duplicates(subset=['id_empresa', 'fecha'], keep='first', inplace=True)
```

**Por qué `keep='first'`:** en el archivo fuente los registros se agregan cronológicamente, por lo que la primera aparición corresponde al dato original y las posteriores a reprocesos. Si en el futuro el CSV incluyera una columna de timestamp de carga, la decisión correcta sería `keep='last'` ordenando por esa columna.

**Impacto medido:** el conteo de filas antes y después de cada `drop_duplicates` se registra en consola para dejar evidencia de cuántos registros se descartaron.

### 2.2 Estandarización de fechas

> **Corrección aplicada.** El código original contenía este bloque:
>
> ```python
> if col == "fecha":
>     pd.to_datetime(df[col], format="mixed", errors="coerce")
> ```
>
> El resultado de `pd.to_datetime` no se asignaba a ninguna variable, por lo que **la columna `fecha` nunca se convertía**: llegaba a PostgreSQL como texto sin normalizar. Además, al no asignarse, la columna caía en el bloque de limpieza de texto, que le aplicaba `.str.replace("-", "")` y destruía el separador de fecha.

Implementación corregida:

```python
df["fecha"] = pd.to_datetime(df["fecha"], format="mixed", errors="coerce")
df["fecha"] = df["fecha"].dt.normalize()   # trunca hora, deja solo la fecha
```

`errors="coerce"` convierte cualquier fecha no parseable en `NaT`, que luego se trata como nulo en columna clave (§3.2). El tipo `datetime64` se traduce automáticamente a `DATE`/`TIMESTAMP` en PostgreSQL vía SQLAlchemy, lo que permite que Power BI genere jerarquías de tiempo sin conversión manual.

### 2.3 Normalización de texto de agrupación

Las columnas categóricas (empresa, periodo, región) se normalizan con una cadena única de transformaciones:

```python
df[col] = (df[col]
           .fillna("DESCONOCIDO")
           .astype(str)
           .str.strip()          # espacios al inicio y final
           .str.replace("-", "", regex=False)
           .str.replace(".", "", regex=False)
           .str.replace(" ", "_", regex=False)
           .str.replace(r"[(, )]", "", regex=True)
           .str.upper())         # criterio único de capitalización
```

**Por qué MAYÚSCULAS y no `title case`:** las mayúsculas son idempotentes y no dependen del locale. `"byd auto"`, `"BYD Auto"` y `" byd  auto "` convergen todos a `BYD_AUTO`, que es exactamente lo que se necesita para que la dimensión `empresas` no genere dos `id_empresa` distintos para la misma compañía.

**Por qué el guion bajo en lugar del espacio:** garantiza que el valor sea seguro como identificador SQL y evita problemas de tokenización en los filtros de Power BI.

### 2.4 Métricas derivadas

El pipeline calcula cuatro métricas de negocio que no existen en el archivo fuente:

```python
df_desempeno = df_desempeno.sort_values(['id_empresa', 'fecha'])
g = df_desempeno.groupby('id_empresa')

# 1. Variación porcentual interanual de ingresos
df_desempeno['var_ingresos_yoy_dec'] = g['ingresos_totales_usd_mn'].pct_change().round(4)

# 2. Variación porcentual interanual del beneficio neto
df_desempeno['var_beneficio_yoy_dec'] = g['beneficio_neto_usd_mn'].pct_change().round(4)

# 3. Margen neto recalculado (control de consistencia contra el dato fuente)
df_desempeno['margen_neto_calc_dec'] = (
    df_desempeno['beneficio_neto_usd_mn'] / df_desempeno['ingresos_totales_usd_mn']
).replace([np.inf, -np.inf], np.nan).round(4)

# 4. Dependencia del subsidio estatal
df_desempeno['dependencia_subsidio_dec'] = (
    df_desempeno['inyecciones_gobierno_usd_mn'] / df_desempeno['beneficio_neto_usd_mn']
).replace([np.inf, -np.inf], np.nan).round(4)
```

Las métricas 1 y 2 permiten construir directamente el visual de crecimiento del dashboard. La métrica 3 es un control de calidad: si `margen_neto_calc_dec` difiere de `margen_neto_dec` del archivo fuente, hay una inconsistencia en el origen. La métrica 4 es la que responde la pregunta central del análisis (§5).

---

## 3. Resolución de casos ambiguos

### 3.1 Valores atípicos e inconsistentes (ceros y negativos)

**El problema.** Un cero puede significar tres cosas distintas: un valor real de cero, un dato faltante que alguien rellenó, o un error de captura. Un negativo puede ser legítimo o un error de signo.

**La decisión: tratamiento diferenciado por columna, no una regla global.**

| Columna | Cero | Negativo | Decisión |
|---|---|---|---|
| `beneficio_neto_usd_mn` | Válido | **Válido** | Se conserva sin modificar. Una pérdida es un dato real y crítico para el análisis. |
| `ingresos_totales_usd_mn` | Inválido | Inválido | Se marca como atípico. Una empresa operativa no factura cero. |
| `unidades_vendidas` | Sospechoso | Inválido | Negativo se marca como atípico; cero se conserva pero se señala. |
| `inyecciones_gobierno_usd_mn` | **Válido** | Inválido | Cero significa "no recibió subsidio ese año" — es información, no ausencia de dato. |
| Columnas `_dec` (ratios) | Válido | Válido | Un margen negativo es legítimo. Solo se marcan valores fuera de `[-1, 1]` en participaciones de mercado. |

**Implementación: marcar, no borrar.**

```python
df_desempeno['flag_calidad'] = 'OK'

cond_ingreso_invalido = df_desempeno['ingresos_totales_usd_mn'] <= 0
cond_unidades_negativas = df_desempeno['unidades_vendidas'] < 0
cond_participacion_fuera_rango = (
    (df_desempeno['cuota_mercado_total_china_dec'] < 0) |
    (df_desempeno['cuota_mercado_total_china_dec'] > 1)
)

df_desempeno.loc[cond_ingreso_invalido, 'flag_calidad'] = 'INGRESO_INVALIDO'
df_desempeno.loc[cond_unidades_negativas, 'flag_calidad'] = 'UNIDADES_NEGATIVAS'
df_desempeno.loc[cond_participacion_fuera_rango, 'flag_calidad'] = 'RATIO_FUERA_RANGO'
```

**Por qué marcar en lugar de excluir.** Excluir la fila silenciosamente rompe la continuidad de la serie temporal: si falta 2021, el cálculo de variación interanual de 2022 se compara contra 2020 y produce un crecimiento inflado. Al marcar, la fila sigue en el repositorio y Power BI puede filtrarla con un segmentador, pero el analista **sabe que existe y por qué se excluyó**.

**Impacto de la decisión.** El dashboard debe aplicar por defecto el filtro `flag_calidad = 'OK'` en los visuales de tendencia. El conteo de filas marcadas se reporta al final de la ejecución; si supera el 5% del total, el problema está en el origen y no debe resolverse en el pipeline.

### 3.2 Nulos en columnas clave

**Columnas clave definidas:** `id_empresa` (identificador), `fecha` (temporal), `ingresos_totales_usd_mn` (métrica principal).

| Columna clave | Decisión | Justificación |
|---|---|---|
| `fecha` nula (`NaT`) | **Descartar la fila** | Sin fecha, el registro no puede ubicarse en ninguna serie temporal ni participar en la llave primaria compuesta. No es recuperable. |
| `id_empresa` nulo | **Descartar la fila** | Violaría la llave foránea. Se descarta antes de la carga. |
| `ingresos_totales_usd_mn` nulo | **Marcar, no imputar** | Ver abajo. |

```python
filas_antes = len(df_desempeno)
df_desempeno = df_desempeno[df_desempeno['fecha'].notna()]
df_desempeno = df_desempeno[df_desempeno['id_empresa'].notna()]
print(f"Descartadas por clave nula: {filas_antes - len(df_desempeno)}")
```

> **Corrección aplicada.** El código original ejecutaba `.fillna(0)` sobre **todas** las columnas numéricas. Esto significa que un año sin dato de beneficio neto se cargaba como `0`, indistinguible de un año en que la empresa efectivamente empató. En un análisis cuyo hallazgo central es una caída del beneficio, imputar ceros contamina directamente la conclusión.
>
> **Decisión corregida:** los nulos en métricas se conservan como `NULL` en PostgreSQL. SQL y Power BI excluyen `NULL` de los promedios de forma automática, mientras que un `0` imputado los arrastra hacia abajo.

```python
# Solo se imputa donde el cero tiene significado de negocio real
df['inyecciones_gobierno_usd_mn'] = df['inyecciones_gobierno_usd_mn'].fillna(0)

# El resto conserva NULL
for col in columna_numericas:
    if col in df.columns and col != 'inyecciones_gobierno_usd_mn':
        df[col] = pd.to_numeric(df[col], errors="coerce").round(0)
```

### 3.3 Registros sin identificador de empresa o producto

**Decisión: incluir con valor centinela explícito, no excluir.**

```python
df['empresa'] = df['empresa'].fillna('DESCONOCIDO')
```

Esto hace que `DESCONOCIDO` reciba su propio `id_empresa` en la dimensión, quedando visible como una entidad más en el modelo.

**Por qué incluir en lugar de excluir.** Si hay registros sin empresa, el volumen de esos registros **es en sí mismo un hallazgo** sobre la calidad del origen. Excluirlos silenciosamente hace que los totales del dashboard no cuadren contra el archivo fuente y nadie pueda explicar la diferencia.

**Por qué un centinela de texto y no `NULL`.** `NULL` no se agrupa: en un `GROUP BY empresa`, las filas nulas desaparecen del resultado o forman un grupo sin etiqueta. `DESCONOCIDO` aparece como una categoría normal en cualquier segmentador de Power BI, y el analista decide si la incluye o la filtra.

**Impacto:** los registros con `empresa = 'DESCONOCIDO'` deben excluirse de la comparación BYD vs. Tesla (no pertenecen a ninguna de las dos), pero se conservan en los totales de calidad de datos.

### 3.4 Variaciones de escritura para un mismo valor

**El problema.** `"BYD"`, `"byd"`, `"BYD Co. Ltd"`, `" BYD "` y `"BYD-AUTO"` son la misma empresa. Si no se unifican, la dimensión genera cinco `id_empresa` distintos y las series se fragmentan en cinco líneas separadas en el dashboard.

**Decisión: normalización en dos capas.**

**Capa 1 — normalización mecánica** (§2.3): resuelve las diferencias de capitalización, espacios y puntuación. Convierte `" byd "`, `"BYD"` y `"Byd."` a `BYD`.

**Capa 2 — diccionario de mapeo explícito** para variantes que la normalización mecánica no puede resolver, porque son nombres realmente distintos que refieren a la misma entidad:

```python
MAPEO_EMPRESAS = {
    'BYD_CO_LTD': 'BYD',
    'BYD_AUTO': 'BYD',
    'BYD_COMPANY': 'BYD',
    'TESLA_INC': 'TESLA',
    'TESLA_MOTORS': 'TESLA',
}
df['empresa'] = df['empresa'].replace(MAPEO_EMPRESAS)
```

**Por qué un diccionario y no fuzzy matching.** El emparejamiento aproximado (`fuzzywuzzy`, `rapidfuzz`) es no determinista frente a datos nuevos: un umbral de similitud que hoy funciona puede fusionar mañana dos empresas distintas con nombres parecidos. En un dataset con pocas entidades, el diccionario explícito es auditable, revisable y no falla en silencio.

**Regla operativa:** la normalización se aplica **antes** de construir `df_empresas`. Si se aplicara después, la dimensión ya habría asignado IDs a las variantes y la corrección llegaría tarde.

### 3.5 Otros defectos corregidos en el código base

**Decimales con doble punto.** El código original contenía:

```python
df[col].astype(str).str.strip().replace("..", ".")
```

`Series.replace` con una cadena literal solo sustituye cuando **el valor completo** es `".."`. Un valor como `"0..1652"` no se corregía y terminaba como `NaN` tras `to_numeric`. Corrección:

```python
df[col] = df[col].astype(str).str.strip().str.replace("..", ".", regex=False)
```

**Inconsistencia de nomenclatura.** `columnas_float` declara `descuento_Implicito_dec` con `I` mayúscula, mientras que `columnas_hechos` usa `descuento_implicito_dec`. Funciona por accidente, porque las cabeceras del CSV pasan por `.str.lower()` antes de la comparación, pero es frágil. Se unificó a minúscula.

**Credenciales en el código.** La cadena de conexión incluía usuario y contraseña literales. Se movieron a variables de entorno:

```python
from dotenv import load_dotenv
import os

load_dotenv()
engine = create_engine(
    f"postgresql+psycopg2://{os.getenv('PG_USER')}:{os.getenv('PG_PASS')}"
    f"@{os.getenv('PG_HOST')}:{os.getenv('PG_PORT')}/{os.getenv('PG_DB')}"
)
```

**Estrategia de recarga.** El pipeline ejecuta `DROP TABLE ... CASCADE` en cada corrida. Esto es una decisión deliberada, no un descuido: el volumen es pequeño y una recarga completa garantiza que el repositorio siempre refleje exactamente el estado del CSV origen, sin estados intermedios inconsistentes. Para volúmenes mayores, la estrategia correcta sería carga incremental con `UPSERT` sobre la llave compuesta.

---

## 4. Arquitectura de despliegue

PostgreSQL corre en contenedor Docker en lugar de instalación local, por tres razones:

1. **Reproducibilidad.** Cualquier evaluador levanta la base con `docker compose up` sin instalar PostgreSQL ni configurar el servicio.
2. **Aislamiento de puerto.** El contenedor expone el `5434` en lugar del `5432` por defecto, evitando conflicto con instalaciones previas en la máquina anfitriona.
3. **Desechabilidad.** Si el estado de la base se corrompe durante el desarrollo, se destruye y recrea en segundos.

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: bd_byd
      POSTGRES_USER: ${PG_USER}
      POSTGRES_PASSWORD: ${PG_PASS}
    ports:
      - "5434:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

Power BI se conecta por el conector nativo de PostgreSQL contra `localhost:5434`, en modo importación (no DirectQuery), dado que el volumen es reducido y los datos se actualizan por lotes, no en tiempo real.

---

## 5. Conclusiones e insights del análisis

### 5.1 El crecimiento es real, pero la rentabilidad va en dirección contraria

El hallazgo central es una **divergencia entre volumen y rentabilidad**. Las ventas crecieron 412,73% en cinco años hasta alcanzar un máximo de USD 116 mil millones, mientras el beneficio neto cayó 16% en el último año. Un crecimiento de esa magnitud acompañado de una caída de utilidad indica que **cada unidad adicional vendida aporta menos margen que la anterior**.

La lectura no es que la empresa esté en problemas, sino que cambió de estrategia: pasó de maximizar rentabilidad a maximizar participación de mercado.

### 5.2 La dependencia del subsidio estatal es el riesgo estructural

Las inyecciones gubernamentales crecieron al mismo ritmo que las ventas (412,73% en cinco años). Esto es el dato más importante del análisis, porque **una parte del resultado operativo no proviene del negocio sino de política pública**. La métrica derivada `dependencia_subsidio_dec` (§2.4) cuantifica exactamente esta exposición: mide qué porcentaje del beneficio neto desaparecería si el subsidio se retirara.

Este es el punto donde el análisis deja de ser descriptivo y se vuelve accionable.

### 5.3 El pasivo crece de forma sostenida

Los pasivos totales muestran crecimiento consistente año contra año. Por sí solo no es alarmante: financiar expansión con deuda es normal. Se vuelve relevante al cruzarlo con el margen decreciente — **si el margen sigue comprimiéndose mientras el pasivo crece, la capacidad de servicio de la deuda se deteriora en ambos frentes simultáneamente**.

### 5.4 Frente a Tesla: dos modelos de negocio distintos

La comparación muestra a BYD superando a Tesla en volumen de unidades, pero con un margen neto inferior. Son estrategias opuestas: Tesla optimiza margen por vehículo, BYD optimiza escala. Ninguna es superior en abstracto — lo relevante es que **BYD necesita mantener el volumen para sostener el resultado absoluto**, lo que la hace más vulnerable a una contracción de demanda que a un competidor con margen alto.

### 5.5 Concentración geográfica

Las columnas `pct_ventas_china_dec` y `pct_ventas_exterior_dec` cuantifican la exposición a un solo mercado. La participación en el mercado chino de NEV es alta, lo que significa que **el crecimiento futuro por esa vía tiene techo**: en un mercado donde ya se es líder, crecer requiere que crezca el mercado entero o salir al exterior. La evolución de `pct_ventas_exterior_dec` es el indicador que anticipa si esa transición está ocurriendo.

---

## 6. Diez preguntas de negocio de alto valor

Las preguntas se ordenan de mayor a menor urgencia estratégica. La columna **Fuente** indica si el dashboard actual ya la responde, si requiere cruzar visuales existentes, o si necesita una medida nueva sobre el repositorio.

| # | Pregunta | Fuente |
|---|---|---|
| 1 | Dependencia del subsidio estatal | Gráfica (inyecciones + beneficio) |
| 2 | Causa de la compresión de margen | Cruce de visuales |
| 3 | Beneficio por unidad vendida | Gráfica (unidades + beneficio) |
| 4 | Sostenibilidad del pasivo | Gráfica (pasivos) |
| 5 | Punto de equilibrio volumen–margen | Medida nueva |
| 6 | Velocidad de internacionalización | Medida nueva |
| 7 | Brecha de margen contra Tesla | Gráfica (comparativo) |
| 8 | Techo de crecimiento en China | Gráfica (participación) |
| 9 | Riesgo diferido de garantías | Medida nueva |
| 10 | Repetibilidad del 412,73% | Gráfica (serie de ventas) |

---

### Pregunta 1 — ¿Cuánto del beneficio neto proviene del subsidio estatal, y qué queda del negocio sin él?

**Se responde con la gráfica.** El visual de inyecciones gubernamentales y el indicador de beneficio neto ya están en el tablero; basta superponerlos en el mismo eje temporal.

Es la primera pregunta que hace un inversionista, un banco o un comité de crédito. Determina si la utilidad reportada es resultado de la operación o transferencia de política pública. El dato que la hace urgente es que las inyecciones crecieron al mismo ritmo que las ventas: si el subsidio se retirara, no se pierde un porcentaje del margen, se pierde el motor que lo sostenía.

**Medida:** `dependencia_subsidio_dec`, más el escenario contrafactual `beneficio_neto_usd_mn - inyecciones_gobierno_usd_mn`. Si ese resultado es negativo en algún año, el hallazgo es material y debe encabezar la presentación.

---

### Pregunta 2 — ¿La caída del 16% en el beneficio viene de descuentos comerciales o de aumento de costos?

**Requiere cruzar visuales.** Los datos ya están cargados, pero el tablero no los enfrenta.

Son dos diagnósticos opuestos con soluciones opuestas. Si es descuento, es una decisión de precios que la empresa puede revertir en un trimestre. Si es costo, es un problema estructural de la operación que tarda años en corregirse. Presentar la caída del 16% sin resolver esta distinción deja la conclusión a medias.

**Medida:** `descuento_implicito_dec` contra `margen_neto_dec` e `ingreso_automotriz_por_vehiculo_usd` en la misma serie. Si el ingreso por vehículo cae junto con el margen, la causa es de precio, no de costo.

---

### Pregunta 3 — ¿Cuánto beneficio genera cada vehículo vendido, y cómo evolucionó esa cifra?

**Se responde con la gráfica.** Los visuales de unidades vendidas y beneficio neto ya contienen ambos términos.

Esta es la traducción más honesta de la divergencia entre el 412,73% de crecimiento y el −16% de beneficio. Un solo número —beneficio por unidad— convierte dos tendencias contradictorias en una tendencia legible, y es la métrica que un comité entiende sin explicación previa.

**Medida:** `beneficio_neto_usd_mn * 1_000_000 / unidades_vendidas` por año. Si la serie es decreciente mientras las unidades suben, la empresa está comprando participación de mercado con utilidad.

---

### Pregunta 4 — ¿El pasivo crece más rápido que la capacidad de pagarlo?

**Se responde con la gráfica.** El visual de pasivos totales ya muestra la trayectoria creciente; falta ponerla en relación con el beneficio.

El pasivo creciente por sí solo no es alarmante — financiar expansión con deuda es normal. Se vuelve crítico al cruzarlo con el margen decreciente: si el margen se comprime mientras el pasivo crece, la capacidad de servicio de la deuda se deteriora por los dos lados a la vez. El tablero muestra ambas tendencias por separado y esa es precisamente la lectura que se pierde.

**Medida:** `pasivo_total_usd_mn / beneficio_neto_usd_mn` en el tiempo. Una pendiente creciente indica que la deuda avanza más rápido que la utilidad que debe cubrirla.

---

### Pregunta 5 — ¿Existe un punto a partir del cual vender más destruye valor?

**Requiere medida nueva.** Es la pregunta más difícil de las diez y la de mayor retorno.

La estrategia actual sacrifica margen deliberadamente para ganar unidades. Eso es racional hasta cierto umbral. Identificar dónde está ese umbral convierte el análisis de descriptivo en prescriptivo: deja de decir *qué pasó* y empieza a decir *hasta dónde conviene seguir*.

**Medida:** regresión de `unidades_vendidas` contra `beneficio_neto_usd_mn` por periodo, buscando el punto donde la pendiente se vuelve negativa. Con pocos años de historia el resultado es indicativo, no concluyente — conviene presentarlo como tal.

---

### Pregunta 6 — ¿A qué velocidad se reduce la dependencia del mercado chino?

**Requiere medida nueva** sobre columnas que ya están en el repositorio.

Con participación ya dominante en China, el crecimiento futuro depende de la internacionalización. La velocidad de esa transición determina si el 412,73% es repetible o irrepetible. Es la pregunta que separa un análisis histórico de una proyección defendible.

**Medida:** tendencia de `pct_ventas_exterior_dec` año contra año, comparada contra la tasa de crecimiento total. Si las ventas crecen más rápido que la participación exterior, la concentración de riesgo está aumentando, no disminuyendo.

---

### Pregunta 7 — ¿Cuál es la brecha de margen frente a Tesla, y se está cerrando o ampliando?

**Se responde con la gráfica.** El visual comparativo BYD vs. Tesla ya contiene ambas series.

La comparación muestra a BYD superando a Tesla en volumen pero con margen inferior. Son modelos de negocio opuestos, y ninguno es superior en abstracto. Lo relevante es la dirección: si la brecha se amplía, BYD está pagando cada punto de participación más caro que antes.

**Medida:** diferencia de `margen_neto_dec` entre ambas empresas por año. La pendiente de esa diferencia es el dato, no su valor absoluto.

---

### Pregunta 8 — ¿Cuánto espacio de crecimiento queda en el mercado chino de NEV?

**Se responde con la gráfica.** El visual de participación de mercado ya muestra el nivel alcanzado.

En un mercado donde ya se es líder, crecer requiere que crezca el mercado entero o salir al exterior. Esta pregunta pone un techo cuantificado al escenario optimista y obliga a que cualquier proyección de crecimiento declare de dónde vendría.

**Medida:** `cuota_mercado_total_china_dec` y `participacion_china_nev_dec` contra `mercado_mundial_ev_unidades`. La diferencia entre la participación actual y un techo realista define el margen de crecimiento orgánico restante.

---

### Pregunta 9 — ¿Las provisiones de garantía cubren el riesgo del parque vendido?

**Requiere medida nueva.** El repositorio tiene las columnas, pero el tablero no las usa.

Cuadruplicar unidades vendidas cuadruplica el parque en circulación y con él la exposición futura a reclamos. Si la provisión no creció en proporción, hay un costo diferido que aún no aparece en el estado de resultados —y que llegaría justo cuando el margen ya está comprimido. Es el riesgo menos visible de todo el análisis.

**Medida:** `provision_garantia_usd_mn` y `reserva_garantia_usd_mn` contra `unidades_vendidas` acumuladas, cruzado con `tasa_reclamos_garantia_dec`. Una provisión por unidad decreciente mientras la tasa de reclamos sube es una señal de alerta.

---

### Pregunta 10 — ¿El crecimiento del 412,73% es base sostenible o pico coyuntural?

**Se responde con la gráfica.** La serie de ventas totales ya muestra la forma de la curva.

Un CAGR compuesto sobre cinco años puede esconder un solo año excepcional. Lo que decide si la cifra sirve como base de proyección es la forma de la curva: crecimiento parejo, aceleración sostenida, o un salto único seguido de estabilización. Presentar el 412,73% sin esta distinción invita a extrapolarlo, que es el error más caro que puede cometer el análisis.

**Medida:** `var_ingresos_yoy_dec` año a año en lugar del acumulado del periodo. Si la variación interanual se desacelera mientras el acumulado sigue impresionando, el pico ya pasó.

---

## 7. Limitaciones conocidas

1. **Los datos provienen de generación asistida por IA**, no de extracción directa de reportes financieros auditados. Antes de usar estas conclusiones para una decisión real, las cifras clave deben verificarse contra los informes anuales publicados por la empresa.
2. **La recarga completa no conserva historial.** Cada ejecución reemplaza el contenido del repositorio; no hay tabla de auditoría que registre versiones anteriores.
3. **El modelo no incluye dimensión de tiempo dedicada.** Para análisis de estacionalidad intra-anual sería necesaria una tabla `dim_fecha` con atributos de trimestre, semestre y periodo fiscal.
4. **No hay pruebas automatizadas.** Las validaciones de calidad se ejecutan pero no fallan la corrida; un pipeline de producción debería abortar si el porcentaje de filas marcadas supera un umbral definido.
