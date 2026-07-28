# ---- builder ----
FROM ros:jazzy-ros-base AS builder

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# 1) apt: build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    libturbojpeg0-dev \
    ffmpeg \
    git \
 && rm -rf /var/lib/apt/lists/*

# 2) Python packages
RUN pip install --no-cache-dir --break-system-packages --ignore-installed \
      "numpy==2.2.6" \
      requests datasets \
      tqdm packaging fsspec \
      psutil \
      asyncpg \
      "pydantic>=2.0.0" \
      "pydantic-settings>=2.0.0" \
 && pip install --no-cache-dir --break-system-packages --ignore-installed \
      --index-url https://download.pytorch.org/whl/cpu \
      "torch==2.10.0" \
      "torchvision==0.25.0" \
 && pip install --no-cache-dir --break-system-packages --ignore-installed \
      --no-deps "lerobot==0.5.1" \
 && pip install --no-cache-dir --break-system-packages --ignore-installed \
      "opencv-python-headless>=4.9.0,<4.13.0" \
      "av>=15.0.0,<16.0.0" \
      "pyarrow>=15.0.0" \
      "imageio>=2.34.0,<3.0.0" \
      "pillow>=10.0.0" \
      "pyyaml>=6.0" \
      rosbags \
      "PyTurboJPEG<2.0" \
      "mcap>=0.0.10" \
      "mcap-ros2-support>=0.5.0"

# 5) Copy app code from the bundled conversion submodule
COPY rosbag2lerobot-svt/conversion /app/conversion
COPY rosbag2lerobot-svt/nas /app/nas
COPY --chmod=0644 rosbag2lerobot-svt/scripts/partition_recordings.py /app/rosbag2lerobot-svt/scripts/partition_recordings.py
COPY rosbag2lerobot-svt/auto_converter.py /app/auto_converter.py
COPY conversion_configs/robots /app/configs/robots
COPY backend /app/backend
COPY --chmod=0644 scripts/__init__.py /app/scripts/__init__.py
COPY --chmod=0644 scripts/recover_conversion.py /app/scripts/recover_conversion.py
COPY --chmod=0644 scripts/split_raw_task_by_metadata.py /app/scripts/split_raw_task_by_metadata.py
COPY --chmod=0755 scripts/partition_raw_by_contract.py /app/scripts/partition_raw_by_contract.py

# ---- runtime ----
FROM ros:jazzy-ros-core AS runtime

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# ROS2 packages required at runtime:
#   rosbag2       — rosbag2_py for MCAP reading
#   geometry-msgs — geometry_msgs.msg.Twist (data_converter.py)
#   sensor-msgs   — sensor_msgs.msg.CompressedImage, JointState
#   nav-msgs      — nav_msgs.msg.Odometry
#   trajectory-msgs — trajectory_msgs.msg.JointTrajectory
RUN apt-get update && apt-get install -y --no-install-recommends \
    ros-jazzy-rosbag2 \
    ros-jazzy-geometry-msgs \
    ros-jazzy-sensor-msgs \
    ros-jazzy-nav-msgs \
    ros-jazzy-trajectory-msgs \
    ffmpeg \
    libturbojpeg \
 && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.12/dist-packages /usr/local/lib/python3.12/dist-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy app code
COPY --from=builder /app /app

# Cleanup
RUN find /usr/local/lib/python3.12/dist-packages \
      \( -type d -name "__pycache__" -o \
         -type d -name "tests" -o \
         -type d -name "test" \) \
      -exec rm -rf {} + 2>/dev/null; \
    find /usr/local/lib/python3.12/dist-packages -name "*.pyc" -delete 2>/dev/null; \
    true

# Import smoke test
RUN bash -c 'source /opt/ros/jazzy/setup.bash && \
PYTHONPATH="/app:${PYTHONPATH}" python3 -c "\
from pathlib import Path; \
import rosbag2_py; print(\"rosbag2_py OK\"); \
from sensor_msgs.msg import CompressedImage; print(\"ROS2 msgs OK\"); \
import lerobot; print(\"lerobot\", lerobot.__version__); \
assert Path(\"/app/conversion/data_creator.py\").exists(); \
import conversion.data_creator; print(\"conversion import OK\"); \
import auto_converter; print(\"auto_converter import OK\"); \
import backend.converter.queue_adapter; print(\"queue_adapter import OK\"); \
from backend.converter.recovery_service import _partition_manifest_builder; \
assert callable(_partition_manifest_builder()); print(\"recovery contract probe OK\") \
"'

# Source ROS2 setup in entrypoint
RUN printf '#!/bin/bash\nsource /opt/ros/jazzy/setup.bash\nexec "$@"\n' > /entrypoint.sh \
 && chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-m", "backend.converter.queue_adapter"]
