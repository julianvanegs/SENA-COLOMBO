#IMPORTS
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text 

#CONEXION CON POSTGRES

engine = create_engine("postgresql+psycopg2://postgres:1974@localhost:5434/bd_byd")


#CREACION DE SEGMENTACION ENTRE FLOAT Y ENTEROS
columnas_float = ["tasa_reclamos_garantia_dec",
"margen_neto_dec",
"participacion_mundial_dec",
"participacion_china_nev_dec",
"cuota_mercado_total_china_dec",
"pct_ventas_china_dec",
"pct_ventas_exterior_dec",
"descuento_Implicito_dec",
"tasa_fx_cny_usd"]

columna_numericas = ["ano",
"inyecciones_gobierno_usd_mn",
"reserva_garantia_usd_mn",
"provision_garantia_usd_mn",
"ingresos_totales_usd_mn",
"beneficio_neto_usd_mn",
"pasivo_total_usd_mn",
"mercado_mundial_ev_unidades",
"ingreso_automotriz_por_vehiculo_usd"]

#CREACION DEL DATAFRAME CON EL CSV  Y LIMPIEZA GENERAL
df = pd.read_csv(r"C:\Users\coder\Downloads\PRUEBADESEMPEÑO\BASEACTUALIZACION.csv")
df.drop_duplicates(inplace=True)
df.columns = df.columns.str.strip().str.lower().str.replace(" ", "_")

#LIMPIEZA DE DATOS FLOAT O NUMERICOS
for col in columna_numericas:
    if col in df.columns:
        df[col] = df[col].astype(str).str.strip().replace(r"[$, ]", "", regex=True)
        df[col] = pd.to_numeric(df[col] ,errors="coerce").round(0).fillna(0)

for col in columnas_float:
    if col in df.columns:
        df[col] = df[col].astype(str).str.strip().replace("..", ".")
        df[col] = pd.to_numeric(df[col] ,errors="coerce").fillna(0.0).round(4)


#LIMPIEZA DE DATOS DE TEXTOS
for col in df.columns:
    if col not in columnas_float +  columna_numericas:
        if col == "fecha":
            pd.to_datetime(df[col], format="mixed", errors="coerce")
        else:
            df[col] = (df[col]
                       .fillna("DESCONOCIDO")
                       .astype(str)
                       .str.strip()
                       .str.replace("-" , "")
                       .str.replace(".", "")
                       .str.replace(" ", "_")
                       .str.replace(r"[(, )]", "", regex=True)
                       .str.upper())



#NORMALIZACION 3N 
df_empresas = df[['empresa']].drop_duplicates().reset_index(drop=True)
df_empresas['id_empresa'] = df_empresas.index + 1

df = df.merge(df_empresas, on='empresa', how='left')


#COLUMNA DE HECHOS
columnas_hechos = [
        'id_empresa', 'fecha', 'periodo', 'unidades_vendidas', 'inyecciones_gobierno_usd_mn',
    'tasa_reclamos_garantia_dec', 'ingresos_totales_usd_mn', 'beneficio_neto_usd_mn',
    'pasivo_total_usd_mn', 'cuota_mercado_total_china_dec', 'participacion_china_nev_dec',
    'descuento_implicito_dec', 'pct_ventas_china_dec', 'pct_ventas_exterior_dec'
]


columnas_hechos = [c for c in columnas_hechos if c in df.columns]

df_desempeno = df[df['fecha'].notna()][columnas_hechos].copy()
df_desempeno.drop_duplicates(subset=['id_empresa', 'fecha'], keep='first', inplace=True)
df_desempeno.reset_index(drop=True, inplace=True)

# =====================================================================
# 🚀 SUBIR A POSTGRESQL (SOLO TABLAS NORMALIZADAS)
# =====================================================================
with engine.connect() as con:
    con.execute(text("DROP TABLE IF EXISTS desempeno_anual CASCADE;"))
    con.execute(text("DROP TABLE IF EXISTS empresas CASCADE;"))
    con.commit()

df_empresas.to_sql('empresas', con=engine, if_exists='replace', index=False)
df_desempeno.to_sql('desempeno_anual', con=engine, if_exists='replace', index=False)

with engine.connect() as con:
    # Agregar Claves Primarias y Foráneas
    con.execute(text("ALTER TABLE empresas ADD PRIMARY KEY (id_empresa);"))
    con.execute(text("ALTER TABLE desempeno_anual ADD PRIMARY KEY (id_empresa, fecha);"))
    con.execute(text("ALTER TABLE desempeno_anual ADD CONSTRAINT fk_empresa FOREIGN KEY (id_empresa) REFERENCES empresas(id_empresa);"))
    con.commit()
print(df)
print("¡Modelo 3FN limpio creado e inyectado con éxito en PostgreSQL!")