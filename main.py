
from fastapi import FastAPI, File, UploadFile, Depends, HTTPException,status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
import auth # Importamos nuestro nuevo archivo
from jose import JWTError, jwt
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

app = FastAPI(title="Agro API", description="MVP de Agricultura de Precisión")
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