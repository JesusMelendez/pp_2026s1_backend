from sqlalchemy import Column, Integer, String, Numeric, ForeignKey, Date, DateTime, Boolean
from sqlalchemy.orm import declarative_base, relationship
from geoalchemy2 import Geometry
from datetime import datetime

# Esta es la clase base de la que heredan todos tus modelos
Base = declarative_base()

# ==========================================
# 1. CATÁLOGOS (Para mantener orden sin complicar)
# ==========================================
class Rol(Base):
    __tablename__ = 'catalogo_rol'
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(50), unique=True, nullable=False)
    
    # Relación bidireccional
    usuarios = relationship("Usuario", back_populates="rol")

class Cultivo(Base):
    __tablename__ = 'catalogo_cultivo'
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), unique=True, nullable=False)

# ==========================================
# 2. USUARIOS (Con Control de Acceso - RBAC)
# ==========================================
class Usuario(Base):
    __tablename__ = 'usuario'
    id = Column(Integer, primary_key=True, index=True)
    rol_id = Column(Integer, ForeignKey('catalogo_rol.id'), nullable=False)
    nombre = Column(String(100), nullable=False)
    email = Column(String(100), unique=True, nullable=False, index=True)
    telefono = Column(String(20), nullable=True) # Opcional para admins/inversores
    password_hash = Column(String(255), nullable=False)
    fecha_registro = Column(DateTime, default=datetime.utcnow)

    # Relaciones
    rol = relationship("Rol", back_populates="usuarios")
    parcelas = relationship("Parcela", back_populates="productor")
    tipo_suscripcion = Column(String(50), default="freemium")
# ==========================================
# 3. LA PARCELA / CICLO (El núcleo de tu MVP)
# ==========================================
class Parcela(Base):
    __tablename__ = 'parcela'
    
    # Datos Generales
    id = Column(Integer, primary_key=True, index=True)
    productor_id = Column(Integer, ForeignKey('usuario.id'), nullable=False)
    nombre_parcela = Column(String(100), nullable=False) # Ej. "La escondida - PV 2026"
    
    # Datos del Ciclo / MVP
    cultivo_id = Column(Integer, ForeignKey('catalogo_cultivo.id'), nullable=False)
    fecha_siembra = Column(Date, nullable=False)
    estado_ciclo = Column(String(50), default="Activo") # Activo, Cosechado, Perdido
    
    # Datos Espaciales (PostGIS)
    # 4326 = Lat/Lon estándar. Usamos Polygon.
    poligono = Column(Geometry(geometry_type='POLYGON', srid=4326), nullable=False)
    area_hectareas = Column(Numeric(5, 2), nullable=True) # Se puede calcular después o pedir al usuario

    # Relaciones
    productor = relationship("Usuario", back_populates="parcelas")
    cultivo = relationship("Cultivo")
    registros_diarios = relationship("RegistroDiario", back_populates="parcela")

# ==========================================
# 4. LA BITÁCORA (Donde escribirá tu ETL)
# ==========================================
class RegistroDiario(Base):
    __tablename__ = 'registro_diario'
    
    id = Column(Integer, primary_key=True, index=True)
    parcela_id = Column(Integer, ForeignKey('parcela.id'), nullable=False)
    fecha = Column(Date, nullable=False)
    
    # Entradas (Sensores / Clima)
    humedad_suelo_pct = Column(Numeric(5, 2), nullable=True)
    temp_clima_c = Column(Numeric(5, 2), nullable=True)
    precipitacion_mm = Column(Numeric(5, 2), nullable=True)
    
    # Salidas (Consumos y ML)
    agua_aplicada_litros = Column(Numeric(10, 2), default=0)
    energia_kwh = Column(Numeric(10, 2), default=0)
    
    # Recomendación LLM
    recomendacion_emitida = Column(String, nullable=True)
    sms_enviado = Column(Boolean, default=False)

    # Relación
    parcela = relationship("Parcela", back_populates="registros_diarios")