# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image the commits were labeled in: its Python and SQLite
# decide which sqlglot tests run, so we keep it because we run them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

# The labeling environment, from PyPI.
COPY requirements.lock /app/requirements.lock
RUN pip install -r /app/requirements.lock

# sqlglot at the pinned commit, from its own repository, with bytecode
# compiled ahead so the read-only run does not pay for it.
COPY tools/fetch_sqlglot.py /app/tools/fetch_sqlglot.py
RUN python /app/tools/fetch_sqlglot.py /app/sqlglot \
 && python -m compileall -q /app/sqlglot/sqlglot /app/sqlglot/tests \
 && chmod -R a+rX /app

# The selector and its maps. The maps were traced from the pinned commit with
# tools/trace_all.py and tools/compile_map.py, and are committed as plain JSON.
COPY br /app/br
COPY maps /app/maps
COPY selector.py /app/selector.py
RUN python -m compileall -q /app/br /app/selector.py && chmod -R a+rX /app

ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
CMD ["python", "/app/selector.py"]
