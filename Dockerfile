# Usamos la imagen base oficial y completa de Python 3.13
FROM python:3.13.5

# Establecemos el directorio de trabajo
WORKDIR /app

# Copiamos solo el bot para tener su contexto
COPY bot.py .

# --- INICIO DE LA MODIFICACIÓN PARA DEPURACIÓN ---
# En lugar de usar requirements.txt, instalamos cada paquete por separado
# para identificar cuál es el que falla.

RUN echo "Paso 1/4: Instalando pyrogram..."
RUN pip install pyrogram

RUN echo "Paso 2/4: Instalando uvicorn..."
RUN pip install "uvicorn[standard]"

RUN echo "Paso 3/4: Instalando fastapi..."
RUN pip install fastapi

RUN echo "Paso 4/4: Instalando pydantic..."
RUN pip install pydantic

RUN echo "¡Todas las dependencias se instalaron con éxito!"
# --- FIN DE LA MODIFICACIÓN ---

# El comando que se ejecutará cuando el contenedor inicie
CMD ["python3", "bot.py"]
