ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

WORKDIR /app
COPY pyproject.toml README.md ./
COPY civ6relay ./civ6relay
RUN pip install --no-cache-dir .

USER nobody
EXPOSE 42056/udp
ENTRYPOINT ["python", "-m", "civ6relay.server"]
CMD ["--listen", "0.0.0.0:42056"]
