# Usamos la imagen base oficial y completa de Python 3.13
FROM python:3.13.5

# Establecemos el directorio de trabajo
WORKDIR /app

# Copiamos solo el bot para tener su contexto
COPY bot-full.py .

# --- INICIO DE LA MODIFICACIÓN PARA DEPURACIÓN ---
# En lugar de usar requirements.txt, instalamos cada paquete por separado
# para identificar cuál es el que falla.

RUN pip install --no-cache-dir --upgrade \
    pyrogram \
    tgcrypto \
    fastapi \
    "uvicorn[standard]" \
    pydantic \
    openrouteservice-py \
    staticmap \
    Pillow \
    pytz

RUN echo "¡Todas las dependencias se instalaron con éxito!"
# --- FIN DE LA MODIFICACIÓN ---

# El comando que se ejecutará cuando el contenedor inicie
CMD ["python3", "bot-full.py"]
