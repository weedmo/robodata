FROM python:3.13-slim

# Pinned to match the app and converter rerun-sdk (0.26.x) so the gRPC protocol
# is compatible — a raw episode streamed from the converter must reach this
# shared viewer. A client/server version skew surfaces as gRPC transport errors.
RUN pip install --no-cache-dir "rerun-sdk>=0.26,<0.27"

# The `rerun` console script is installed by rerun-sdk; expose both ports.
EXPOSE 9876 9090

# Command is overridden from docker/compose.yml so flags stay visible there.
CMD ["rerun", "--help"]
