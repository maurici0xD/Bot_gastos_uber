# Usamos la imagen base oficial y completa de Python 3.13
# Esto incluye las herramientas de compilación necesarias para la mayoría de los paquetes.
FROM python:3.13.5

# Establecemos el directorio de trabajo dentro del contenedor
WORKDIR /app

# Copiamos primero el archivo de requerimientos
COPY requirements.txt .

# Instalamos las dependencias de Python
# Con la imagen completa, esto debería funcionar sin problemas.
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos el resto de nuestro código al contenedor
COPY bot.py .

# El comando que se ejecutará cuando el contenedor inicie
CMD ["python3", "bot.py"]
