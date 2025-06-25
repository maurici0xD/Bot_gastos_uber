# Usamos una imagen base oficial y ligera de Python 3.13
FROM python:3.13-slim

# --- MODIFICACIÓN: Instalar herramientas de compilación del sistema ---
# Esto es necesario para que 'pip' pueda instalar paquetes que requieren compilación.
RUN apt-get update && apt-get install -y build-essential

# Establecemos el directorio de trabajo dentro del contenedor
WORKDIR /app

# Copiamos primero el archivo de requerimientos
COPY requirements.txt .

# Instalamos las dependencias de Python
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos el resto de nuestro código al contenedor
# --- MODIFICACIÓN: Usar el nombre de archivo correcto ---
COPY bot.py .

# El comando que se ejecutará cuando el contenedor inicie
# --- MODIFICACIÓN: Usar el nombre de archivo correcto ---
CMD ["python3", "bot.py"]
