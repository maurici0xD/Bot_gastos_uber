# Dockerfile

# Usamos una imagen base oficial y ligera de Python 3.13
FROM python:3.13-slim

# Establecemos el directorio de trabajo dentro del contenedor
WORKDIR /app

# Copiamos primero el archivo de requerimientos
COPY requirements.txt .

# Instalamos las dependencias de Python
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos el resto de nuestro código al contenedor
COPY . .

# El comando que se ejecutará cuando el contenedor inicie
CMD ["python3", "bot.py"]