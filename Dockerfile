FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py student_access.py run_bundle.py run_imports.py grading.json ./
COPY static/ static/
ENV PLANA_DATA_DIR=/var/lib/plana PLANA_IMPORTS_DIR=/var/lib/plana/imports PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "app.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
