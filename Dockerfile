FROM python:3.11-slim

# Evita que Python genere archivos .pyc y fuerza la salida de logs en tiempo real
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copiar e instalar dependencias primero
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del código del proyecto
COPY . .

# Comando de inicio del bot
CMD ["python", "main.py"]