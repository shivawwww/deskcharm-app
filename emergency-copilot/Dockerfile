FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV COPILOT_VAR=/data HOST=0.0.0.0 PORT=8080
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "copilot"]
