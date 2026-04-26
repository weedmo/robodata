FROM python:3.13-slim

RUN pip install --no-cache-dir "rerun-sdk>=0.22.0,<0.23.0"

# The `rerun` console script is installed by rerun-sdk; expose both ports.
EXPOSE 9876 9090

# Command is overridden from docker/compose.yml so flags stay visible there.
CMD ["rerun", "--help"]
