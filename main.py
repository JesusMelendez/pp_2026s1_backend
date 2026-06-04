
from fastapi import FastAPI, File, UploadFile, Depends, HTTPException,status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
import auth # Importamos nuestro nuevo archivo
from jose import JWTError, jwt
import os
from pathlib import Path
import ee
from contextlib import asynccontextmanager

# 1. Encontramos la ruta absoluta de la carpeta donde está este archivo main.py
BASE_DIR = Path(__file__).resolve().parent

# 2. Apuntamos a la carpeta secrets/gee-key.json de forma relativa a la raíz
RUTA_KEY_LOCAL = BASE_DIR / "secrets" / "gee-keys.json"
credenciales = ee.ServiceAccountCredentials(
    None,
    key_file=str(RUTA_KEY_LOCAL)   # ← key_file acepta la ruta
)
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        if RUTA_KEY_LOCAL.exists():
            credenciales = ee.ServiceAccountCredentials(
                None,
                key_file=str(RUTA_KEY_LOCAL)  # ✅ ruta del archivo
            )
            ee.Initialize(credenciales)
            print("✅ [GEE] Conectado a Google Earth Engine con éxito (Modo Local).")
        else:
            print(f"⚠️ [GEE] No se encontró el archivo en: {RUTA_KEY_LOCAL}")
    except Exception as e:
        print(f"❌ [GEE] Error crítico de inicialización: {str(e)}")

    yield
    print("🔌 [GEE] Desconectando servicios...")

# --- CONFIGURACIÓN DE SEGURIDAD ---
# Esto le dice a FastAPI dónde tienen que ir los usuarios para conseguir su token
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")
def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        # Intentamos descifrar el token usando tu llave secreta
        payload = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise HTTPException(status_code=401, detail="Token inválido")
        # Si todo sale bien, devolvemos los datos del usuario
        return payload 
    except JWTError:
        raise HTTPException(status_code=401, detail="Firma de token inválida o expirada")


# pyrefly: ignore [missing-import]

from sqlalchemy.orm import Session
import pandas as pd
from io import BytesIO

from database import SessionLocal, engine
import models # Asegúrate de tener el código de tus tablas guardado en models.py

# 1. Esto le dice a SQLAlchemy que cree las tablas en PostgreSQL si no existen
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Agro API", description="MVP de Agricultura de Precisión", lifespan=lifespan)
origenes_permitidos = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origenes_permitidos, 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# =======

# 2. Dependencia para abrir y cerrar la conexión a la BD por cada petición
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 3. El Endpoint mágico para cargar CSVs
@app.post("/upload-cultivos/")
async def upload_csv_cultivos(file: UploadFile = File(...),usuario_actual: dict = Depends(get_current_user)):
    # Validamos que sea un CSV
    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="El archivo debe ser formato .csv")
    
    try:
        # Leemos el archivo asíncronamente a la memoria
        contents = await file.read()
        
        # Lo pasamos a Pandas (BytesIO simula un archivo físico en RAM)
        df = pd.read_csv(BytesIO(contents))
        
        # --- AQUÍ VA TU LÓGICA DE LIMPIEZA CON PANDAS ---
        # df = df.dropna()
        # df['nombre'] = df['nombre'].str.upper()
        
        # Inyectamos el DataFrame directo a PostgreSQL
        # if_exists='append' agrega los datos sin borrar la tabla
        df.to_sql('catalogo_cultivo', con=engine, if_exists='append', index=False)
        
        return {
            "mensaje": "Carga masiva exitosa", 
            "filas_insertadas": len(df),
            "columnas_detectadas": list(df.columns)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando el archivo: {str(e)}")

# --- ESQUEMA DE DATOS (Pydantic) ---
class UsuarioCreate(BaseModel):
    nombre: str
    email: str
    password: str = Field(..., max_length=72)
    rol_id: int = 1 # Por defecto será 1 (Productor)

# --- NUEVOS ENDPOINTS ---

@app.post("/registro/")
def registrar_usuario(usuario: UsuarioCreate, db: Session = Depends(get_db)):
    # 1. Revisamos que el correo no exista ya en la BD
    usuario_existente = db.query(models.Usuario).filter(models.Usuario.email == usuario.email).first()
    if usuario_existente:
        raise HTTPException(status_code=400, detail="El email ya está registrado")
    
    # 2. Encriptamos la contraseña y guardamos
    hashed_pwd = auth.get_password_hash(usuario.password)
    nuevo_usuario = models.Usuario(
        nombre=usuario.nombre,
        email=usuario.email,
        password_hash=hashed_pwd,
        rol_id=usuario.rol_id
    )
    
    db.add(nuevo_usuario)
    db.commit()
    db.refresh(nuevo_usuario)
    
    return {"mensaje": "Usuario creado con éxito", "usuario_id": nuevo_usuario.id}

@app.post("/login/")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # 1. Buscamos al usuario por su email (username)
    user = db.query(models.Usuario).filter(models.Usuario.email == form_data.username).first()
    
    # 2. Verificamos que exista y que la contraseña coincida
    if not user or not auth.verify_password(form_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 3. Generamos su Token JWT con su ID adentro
    access_token = auth.create_access_token(
        data={"sub": user.email, "user_id": user.id, "rol_id": user.rol_id}
    )
    
    return {"access_token": access_token, "token_type": "bearer"}

from geoalchemy2.shape import from_shape
from shapely.geometry import shape

@app.post("/parcelas/")
async def crear_parcela(
    datos_parcela: dict, # Recibiremos el GeoJSON que envía ArcGIS
    db: Session = Depends(get_db),
    usuario_actual: dict = Depends(get_current_user)
):
    try:
        # Extraemos la geometría y las propiedades del JSON
        geometria = shape(datos_parcela['geometry'])
        propiedades = datos_parcela['properties']
        
        nueva_parcela = models.Parcela(
            productor_id=usuario_actual['user_id'],
            nombre_parcela=propiedades['nombre'],
            cultivo_id=propiedades['cultivo_id'],
            fecha_siembra=propiedades['fecha_siembra'],
            # Convertimos la geometría de Shapely a formato PostGIS (WKB)
            poligono=from_shape(geometria, srid=4326)
        )
        
        db.add(nueva_parcela)
        db.commit()
        return {"mensaje": "Parcela guardada con éxito"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error en datos geográficos: {str(e)}")

import json
from sqlalchemy import func

@app.get("/parcelas/", response_model=dict)
def obtener_parcelas(
    db: Session = Depends(get_db),
    usuario_actual: dict = Depends(get_current_user)
):
    # 1. Consultamos las parcelas del usuario logueado
    # Usamos func.ST_AsGeoJSON para que Postgres nos de el JSON de la geometría directamente
    query = db.query(
        models.Parcela.id,
        models.Parcela.nombre_parcela,
        models.Parcela.cultivo_id,
        models.Parcela.area_hectareas,
        func.ST_AsGeoJSON(models.Parcela.poligono).label("geometria")
    ).filter(models.Parcela.productor_id == usuario_actual['user_id']).all()

    # 2. Estructuramos el resultado como un FeatureCollection de GeoJSON
    features = []
    for p in query:
        feature = {
            "type": "Feature",
            "geometry": json.loads(p.geometria),
            "properties": {
                "id": p.id,
                "nombre": p.nombre_parcela,
                "cultivo_id": p.cultivo_id,
                "area": float(p.area_hectareas) if p.area_hectareas else 0
            }
        }
        features.append(feature)

    return {
        "type": "FeatureCollection",
        "features": features
    }

from enum import Enum
from datetime import datetime, timedelta




# ─── Configuración centralizada de capas ───────────────────────────────────────
CAPAS_CONFIG = {
    "rgb": {
        "nombre": "Color Real",
        "dataset": "COPERNICUS/S2_SR_HARMONIZED",
        "ventana_dias": 45,
        "latencia_dias": 2,
        "buffer_m": 0,
        "descripcion": "Imagen satelital verdadero color",
        "leyenda": None  # RGB no tiene escala de valores
    },
    "ndvi": {
        "nombre": "Vegetación (NDVI)",
        "dataset": "COPERNICUS/S2_SR_HARMONIZED",
        "ventana_dias": 45,
        "latencia_dias": 2,
        "buffer_m": 0,
        "descripcion": "Índice de salud y densidad vegetal",
        "leyenda": {
            "unidad": "Índice (-1 a 1)",
            "intervalos": [
                {"min": -1.0, "max": 0.0,  "color": "#FFFFFF", "etiqueta": "Sin vegetación"},
                {"min": 0.0,  "max": 0.2,  "color": "#CE7E00", "etiqueta": "Suelo desnudo / muy escasa"},
                {"min": 0.2,  "max": 0.4,  "color": "#F1C232", "etiqueta": "Vegetación escasa"},
                {"min": 0.4,  "max": 0.6,  "color": "#93C47D", "etiqueta": "Vegetación moderada"},
                {"min": 0.6,  "max": 1.0,  "color": "#38761D", "etiqueta": "Vegetación densa / saludable"},
            ]
        }
    },
    "ndwi": {
        "nombre": "Estrés Hídrico (NDWI)",
        "dataset": "COPERNICUS/S2_SR_HARMONIZED",
        "ventana_dias": 45,
        "latencia_dias": 2,
        "buffer_m": 0,
        "descripcion": "Contenido de agua en la vegetación",
        "leyenda": {
            "unidad": "Índice",
            "intervalos": [
                {"min": -0.1, "max": 0.1,  "color": "#E74C3C", "etiqueta": "Estrés hídrico severo"},
                {"min": 0.1,  "max": 0.2,  "color": "#F1C40F", "etiqueta": "Estrés moderado"},
                {"min": 0.2,  "max": 0.35, "color": "#3498DB", "etiqueta": "Humedad adecuada"},
                {"min": 0.35, "max": 0.5,  "color": "#2980B9", "etiqueta": "Alta disponibilidad hídrica"},
            ]
        }
    },
    "ndre": {
        "nombre": "Estrés Temprano (NDRE)",
        "dataset": "COPERNICUS/S2_SR_HARMONIZED",
        "ventana_dias": 45,
        "latencia_dias": 2,
        "buffer_m": 0,
        "descripcion": "Detecta estrés antes que el NDVI",
        "leyenda": {
            "unidad": "Índice",
            "intervalos": [
                {"min": 0.0,  "max": 0.1,  "color": "#FFFFFF", "etiqueta": "Sin actividad clorofílica"},
                {"min": 0.1,  "max": 0.2,  "color": "#F4A261", "etiqueta": "Estrés alto"},
                {"min": 0.2,  "max": 0.35, "color": "#2A9D8F", "etiqueta": "Estrés leve"},
                {"min": 0.35, "max": 0.5,  "color": "#264653", "etiqueta": "Cultivo saludable"},
            ]
        }
    },
    "evi": {
        "nombre": "Vegetación Mejorada (EVI)",
        "dataset": "COPERNICUS/S2_SR_HARMONIZED",
        "ventana_dias": 45,
        "latencia_dias": 2,
        "buffer_m": 0,
        "descripcion": "Mejor que NDVI en cultivos muy densos",
        "leyenda": {
            "unidad": "Índice (0 a 1)",
            "intervalos": [
                {"min": 0.0,  "max": 0.2,  "color": "#CE7E00", "etiqueta": "Vegetación muy escasa"},
                {"min": 0.2,  "max": 0.4,  "color": "#F1C232", "etiqueta": "Vegetación escasa"},
                {"min": 0.4,  "max": 0.6,  "color": "#6AA84F", "etiqueta": "Vegetación moderada"},
                {"min": 0.6,  "max": 1.0,  "color": "#274E13", "etiqueta": "Vegetación densa"},
            ]
        }
    },
    "soil_moisture": {
        "nombre": "Humedad del Suelo",
        "dataset": "NASA/SMAP/SPL4SMGP/008",
        "ventana_dias": 30,
        "latencia_dias": 7,
        "buffer_m": 25000,
        "descripcion": "Humedad volumétrica superficial (dato regional ~25km)",
        "leyenda": {
            "unidad": "m³/m³",
            "intervalos": [
                {"min": 0.0,  "max": 0.1,  "color": "#8B4513", "etiqueta": "Suelo muy seco"},
                {"min": 0.1,  "max": 0.2,  "color": "#D2B48C", "etiqueta": "Suelo seco"},
                {"min": 0.2,  "max": 0.3,  "color": "#FFFFE0", "etiqueta": "Humedad moderada"},
                {"min": 0.3,  "max": 0.4,  "color": "#00BFFF", "etiqueta": "Suelo húmedo"},
                {"min": 0.4,  "max": 0.5,  "color": "#0000FF", "etiqueta": "Suelo saturado"},
            ]
        }
    },
    "lst": {
        "nombre": "Temperatura del Suelo",
        "dataset": "MODIS/061/MOD11A1",
        "ventana_dias": 16,
        "latencia_dias": 2,
        "buffer_m": 2000,
        "descripcion": "Temperatura superficial diurna",
        "leyenda": {
            "unidad": "°C",
            "intervalos": [
                {"min": 10, "max": 20, "color": "#313695", "etiqueta": "Frío (< 20°C)"},
                {"min": 20, "max": 28, "color": "#74ADD1", "etiqueta": "Templado"},
                {"min": 28, "max": 35, "color": "#FEE090", "etiqueta": "Cálido"},
                {"min": 35, "max": 42, "color": "#F46D43", "etiqueta": "Muy cálido"},
                {"min": 42, "max": 50, "color": "#A50026", "etiqueta": "Estrés térmico (> 42°C)"},
            ]
        }
    },
    "precipitacion": {
        "nombre": "Precipitación Acumulada",
        "dataset": "UCSB-CHG/CHIRPS/DAILY",
        "ventana_dias": 30,
        "latencia_dias": 21,
        "buffer_m": 15000,
        "descripcion": "Lluvia acumulada del período (dato regional ~15km)",
        "leyenda": {
            "unidad": "mm acumulados",
            "intervalos": [
                {"min": 0,   "max": 25,  "color": "#FFFFFF", "etiqueta": "Sin lluvia / muy escasa"},
                {"min": 25,  "max": 75,  "color": "#C6DBEF", "etiqueta": "Lluvia ligera"},
                {"min": 75,  "max": 125, "color": "#6BAED6", "etiqueta": "Lluvia moderada"},
                {"min": 125, "max": 175, "color": "#2171B5", "etiqueta": "Lluvia intensa"},
                {"min": 175, "max": 200, "color": "#08306B", "etiqueta": "Lluvia muy intensa"},
            ]
        }
    },
    # ── NUEVA CAPA ─────────────────────────────────────────────────────────────
    "textura_suelo": {
        "nombre": "Textura del Suelo",
        "dataset": "OpenLandMap/SOL/SOL_TEXTURE-CLASS_USDA-TT_M/v02",
        "ventana_dias": None,   # Dataset estático, no tiene fechas
        "latencia_dias": None,
        "buffer_m": 5000,
        "descripcion": "Clasificación USDA de textura superficial del suelo (dato estático)",
        "leyenda": {
            "unidad": "Clase USDA",
            "intervalos": [
                {"valor": 1,  "color": "#D5C36B", "etiqueta": "Arcilla"},
                {"valor": 2,  "color": "#B96947", "etiqueta": "Arcilla arenosa"},
                {"valor": 3,  "color": "#9D3706", "etiqueta": "Arcilla limosa"},
                {"valor": 4,  "color": "#AE868F", "etiqueta": "Arcillo limosa"},
                {"valor": 5,  "color": "#F86714", "etiqueta": "Franco arcillosa"},
                {"valor": 6,  "color": "#46D143", "etiqueta": "Franco arcillo arenosa"},
                {"valor": 7,  "color": "#368F20", "etiqueta": "Franco"},
                {"valor": 8,  "color": "#3717AA", "etiqueta": "Franco limoso"},
                {"valor": 9,  "color": "#E5218C", "etiqueta": "Franco arenoso"},
                {"valor": 10, "color": "#7517AA", "etiqueta": "Limo"},
                {"valor": 11, "color": "#D2D2D2", "etiqueta": "Arena franca"},
                {"valor": 12, "color": "#F3F3F3", "etiqueta": "Arena"},
            ]
        }
    },
}

class TipoCapa(str, Enum):
    rgb           = "rgb"
    ndvi          = "ndvi"
    ndwi          = "ndwi"
    ndre          = "ndre"
    evi           = "evi"
    soil_moisture = "soil_moisture"
    lst           = "lst"
    precipitacion = "precipitacion"
    textura_suelo = "textura_suelo"  # ✅ faltaba esta línea


def construir_imagen_gee(layer_type: str, gee_geometry, fecha_inicio: str, fecha_fin: str):
    """
    Retorna (imagen, viz_params) según la capa solicitada.
    Lanza HTTPException si no hay datos disponibles.
    """
    config = CAPAS_CONFIG[layer_type]
    
    # ✅ Geometría de trabajo según resolución del dataset
    if config["buffer_m"] > 0:
        # Datasets gruesos: expandir desde el centroide
        area_trabajo = gee_geometry.centroid(maxError=1).buffer(config["buffer_m"])
    else:
        # Datasets finos (Sentinel-2): usar polígono exacto
        area_trabajo = gee_geometry

    # ── Sentinel-2 compartido ──────────────────────────────────────────────────
    if layer_type in ("rgb", "ndvi", "ndwi", "ndre", "evi"):
        coleccion = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                     .filterBounds(area_trabajo)
                     .filterDate(fecha_inicio, fecha_fin)
                     .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 15)))

        if coleccion.size().getInfo() == 0:
            raise HTTPException(
                status_code=404,
                detail=f"Sin imágenes Sentinel-2 limpias para '{layer_type}' en este período. Intenta más tarde."
            )

        s2 = coleccion.median().clip(area_trabajo)

        if layer_type == "rgb":
            return s2, {"bands": ["B4", "B3", "B2"], "min": 0, "max": 3000}

        elif layer_type == "ndvi":
            img = s2.normalizedDifference(["B8", "B4"]).rename("NDVI")
            return img, {
                "min": 0, "max": 1,
                "palette": ["#FFFFFF", "#CE7E00", "#F1C232", "#93C47D", "#38761D"]
            }

        elif layer_type == "ndwi":
            img = s2.normalizedDifference(["B8", "B11"]).rename("NDWI")
            return img, {
                "min": -0.1, "max": 0.5,
                "palette": ["#E74C3C", "#F1C40F", "#3498DB", "#2980B9"]
            }

        elif layer_type == "ndre":
            # Red Edge: detecta clorofila antes de que el NDVI lo muestre
            img = s2.normalizedDifference(["B8", "B5"]).rename("NDRE")
            return img, {
                "min": 0, "max": 0.5,
                "palette": ["#FFFFFF", "#FDE8D8", "#F4A261", "#2A9D8F", "#264653"]
            }

        elif layer_type == "evi":
            # EVI = 2.5 * (B8 - B4) / (B8 + 6*B4 - 7.5*B2 + 1)
            img = s2.expression(
                "2.5 * ((NIR - RED) / (NIR + 6 * RED - 7.5 * BLUE + 1))",
                {"NIR": s2.select("B8"), "RED": s2.select("B4"), "BLUE": s2.select("B2")}
            ).rename("EVI")
            return img, {
                "min": 0, "max": 1,
                "palette": ["#FFFFFF", "#CE7E00", "#F1C232", "#6AA84F", "#274E13"]
            }

    # ── SMAP — Humedad del suelo ───────────────────────────────────────────────
    elif layer_type == "soil_moisture":
        # SMAP 9km — necesita área grande, no el polígono de la parcela
        area_smap = gee_geometry.centroid(maxError=1).buffer(25000)  # 25km radio
        
        # SMAP /008 tiene latencia ~7 días + actualizamos dataset
        fecha_fin_smap    = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        fecha_inicio_smap = (datetime.now() - timedelta(days=37)).strftime("%Y-%m-%d") 

        coleccion = (ee.ImageCollection("NASA/SMAP/SPL4SMGP/008")
                    .filterBounds(area_smap)          # ← area_smap, NO gee_geometry
                    .filterDate(fecha_inicio_smap, fecha_fin_smap))

        # print(f"[SMAP] Imágenes encontradas: {coleccion.size().getInfo()}")  # debug
        print(f"[SMAP] {fecha_inicio_smap} → {fecha_fin_smap}: {coleccion.size().getInfo()} imágenes")

        if coleccion.size().getInfo() == 0:
            raise HTTPException(status_code=404, detail="Sin datos SMAP recientes.")

        img = (coleccion
            .sort("system:time_start", False)
            .first()
            .select("sm_surface")
            .clip(area_smap))                       # ← area_smap
        return img, {
            "min": 0.0, "max": 0.5,
            "palette": ["#8B4513", "#D2B48C", "#FFFFE0", "#00BFFF", "#0000FF"]
        }
      #---------- textura del suelo -----------------# 
    elif layer_type == "textura_suelo":
        area_textura = gee_geometry.centroid(maxError=1).buffer(5000)

        # Dataset estático — no usa filterDate
        img = (ee.Image("OpenLandMap/SOL/SOL_TEXTURE-CLASS_USDA-TT_M/v02")
            .select("b0")  # Capa superficial 0-5cm
            .clip(area_textura))

        # Paleta según clases USDA (12 clases)
        return img, {
            "min": 1, "max": 12,
            "palette": [
                "#D5C36B", "#B96947", "#9D3706", "#AE868F",
                "#F86714", "#46D143", "#368F20", "#3717AA",
                "#E5218C", "#7517AA", "#D2D2D2", "#F3F3F3"
            ]
        }
    # ── MODIS — Temperatura superficial ───────────────────────────────────────
    elif layer_type == "lst":
        coleccion = (ee.ImageCollection("MODIS/061/MOD11A1")
                     .filterBounds(area_trabajo)
                     .filterDate(fecha_inicio, fecha_fin)
                     .select("LST_Day_1km"))

        if coleccion.size().getInfo() == 0:
            raise HTTPException(status_code=404, detail="Sin datos MODIS LST disponibles.")

        # Convertir de Kelvin (×0.02) a Celsius
        img = (coleccion.mean()
               .multiply(0.02)
               .subtract(273.15)
               .clip(area_trabajo))
        return img, {
            "min": 10, "max": 45,
            "palette": ["#313695", "#74ADD1", "#FEE090", "#F46D43", "#A50026"]
        }

    # ── CHIRPS — Precipitación acumulada ──────────────────────────────────────
    elif layer_type == "precipitacion":
        # CHIRPS 5.5km — igual, necesita área extendida
        area_chirps = gee_geometry.centroid(maxError=1).buffer(15000)  # 15km radio
 # CHIRPS tiene latencia ~3 semanas
        fecha_fin_chirps    = (datetime.now() - timedelta(days=21)).strftime("%Y-%m-%d")
        fecha_inicio_chirps = (datetime.now() - timedelta(days=51)).strftime("%Y-%m-%d")  # 30 días de ventana
        coleccion = (ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
                    .filterBounds(area_chirps)        # ← area_chirps, NO gee_geometry
                    .filterDate(fecha_inicio_chirps, fecha_fin_chirps)
                    .select("precipitation"))

        # print(f"[CHIRPS] Imágenes encontradas: {coleccion.size().getInfo()}")  # debug
        print(f"[CHIRPS] {fecha_inicio_chirps} → {fecha_fin_chirps}: {coleccion.size().getInfo()} imágenes")


        if coleccion.size().getInfo() == 0:
            raise HTTPException(status_code=404, detail="Sin datos CHIRPS.")

        img = coleccion.sum().clip(area_chirps)        # ← area_chirps
        return img, {
            "min": 0, "max": 200,
            "palette": ["#FFFFFF", "#C6DBEF", "#6BAED6", "#2171B5", "#08306B"]
        }

    raise HTTPException(status_code=400, detail=f"Capa '{layer_type}' no implementada.")


# ─── Endpoint actualizado ──────────────────────────────────────────────────────
@app.get("/parcelas/{parcela_id}/layers/{layer_type}")
def obtener_capa_satelital(
    parcela_id: int,
    layer_type: TipoCapa,
    db: Session = Depends(get_db),
    usuario_actual: dict = Depends(get_current_user)
):
    parcela = db.query(
        models.Parcela.id,
        func.ST_AsGeoJSON(models.Parcela.poligono).label("geometria")
    ).filter(
        models.Parcela.id == parcela_id,
        models.Parcela.productor_id == usuario_actual["user_id"]
    ).first()

    if not parcela:
        raise HTTPException(status_code=404, detail="Parcela no encontrada.")

    gee_geometry = ee.Geometry(json.loads(parcela.geometria))

    # Ventana de tiempo según config de la capa
    config = CAPAS_CONFIG[layer_type]
    fecha_fin    = datetime.now().strftime("%Y-%m-%d")
    fecha_inicio = (
        (datetime.now() - timedelta(days=config["ventana_dias"])).strftime("%Y-%m-%d")
        if config["ventana_dias"] is not None
        else None
    )
    try:
        imagen, viz_params = construir_imagen_gee(layer_type, gee_geometry, fecha_inicio, fecha_fin)
        map_id_dict = imagen.getMapId(viz_params)

        return {
            "parcela_id": parcela_id,
            "capa_solicitada": layer_type,
            "nombre_capa": config["nombre"],
            "descripcion": config["descripcion"],
            "periodo": {
                "desde": fecha_inicio,
                "hasta": fecha_fin
            } if fecha_inicio is not None else None,        # ✅ usa fecha_inicio, no ventana_dias
            "region_expandida": config["buffer_m"] > 0,
            "buffer_km": config["buffer_m"] / 1000,         # ✅ faltaba este campo
            "leyenda": config["leyenda"],
            "tile_url": map_id_dict["tile_fetcher"].url_format
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error GEE [{layer_type}]: {str(e)}")