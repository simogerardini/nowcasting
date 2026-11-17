# ---- build stage: compile wheels needing a C toolchain (pysteps Cython ext)
FROM python:3.11-bookworm AS build
WORKDIR /wheels
COPY requirements.txt .
RUN pip install --upgrade pip && pip wheel --wheel-dir /wheels -r requirements.txt \
    && mkdir -p /syslib && cp -L /usr/lib/*-linux-gnu/libexpat.so.1 /usr/lib/*-linux-gnu/libgomp.so.1 /syslib/

# ---- runtime: rasterio/shapely/pyproj/opencv wheels bundle GDAL, GEOS, PROJ
FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2
WORKDIR /app
COPY --from=build /wheels /wheels
# system libs absent from slim: libexpat (rasterio), libgomp (pysteps OpenMP)
COPY --from=build /syslib/ /usr/local/lib/
RUN pip install --no-index --find-links=/wheels /wheels/*.whl && rm -rf /wheels && ldconfig
COPY . .
RUN useradd -m -u 1000 app && mkdir -p /data && chown -R app /data /app
USER app
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://localhost:%s/api/v1/health'%os.environ.get('PORT','7860'),timeout=4)" || exit 1
CMD ["./start.sh"]
