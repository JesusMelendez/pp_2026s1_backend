from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Credenciales de tu docker-compose
# Formato: postgresql://usuario:password@host:puerto/nombre_bd
SQLALCHEMY_DATABASE_URL = "postgresql://admin:superpassword123@localhost:5432/agro_db"

# El 'engine' es el motor que ejecuta las consultas
engine = create_engine(SQLALCHEMY_DATABASE_URL)

# SessionLocal es la "fábrica" de sesiones para interactuar con la BD
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)