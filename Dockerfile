# Usamos la imagen base oficial y completa de Python 3.13
FROM python:3.13.5

# Establecemos el directorio de trabajo
WORKDIR /app

# Copiamos solo el bot para tener su contexto
COPY bot-full.py .

# --- INICIO DE LA MODIFICACIÓN PARA DEPURACIÓN ---
# En lugar de usar requirements.txt, instalamos cada paquete por separado
# para identificar cuál es el que falla.

RUN echo "Paso 1/9: Instalando pyrogram..."
RUN pip install -U pyrogram tgcrypto

RUN echo "Paso 3/9: Instalando fastapi..."
RUN pip install fastapi

RUN echo "Paso 4/9: Instalando uvicorn..."
RUN pip install "uvicorn[standard]"

RUN echo "Paso 5/9: Instalando pydantic..."
RUN pip install pydantic

RUN echo "Paso 6/9: Instalando openrouteservice-py..."
RUN pip install openrouteservice

RUN echo "Paso 7/9: Instalando staticmap..."
RUN pip install staticmap

RUN echo "Paso 8/9: Instalando Pillow..."
RUN pip install Pillow

RUN echo "Paso 9/9: Instalando pytz..."
RUN pip install pytz

RUN echo "¡Todas las dependencias se instalaron con éxito!"
# --- FIN DE LA INSTALACIÓN INDIVIDUAL ---

# El comando que se ejecutará cuando el contenedor inicie
CMD ["python3", "bot-full.py"]
