# LeRobot Curation Tools

Local fullstack tool for curating [LeRobot](https://github.com/huggingface/lerobot) datasets. Visualize episodes in [Rerun](https://rerun.io/), assign grades/tags, and edit task instructions — all persisted directly to the dataset's parquet files.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│  React SPA  │────>│   FastAPI    │────>│ Local Dataset   │
│  :5173      │     │   :8000      │     │ (parquet files) │
└─────────────┘     └──────┬───────┘     └─────────────────┘
                           │
                    ┌──────▼───────┐
                    │ Rerun Viewer │
                    │ gRPC :9876   │
                    │ Web  :9090   │
                    └──────────────┘
```

| Component | Tech | Port |
|-----------|------|------|
| Backend | FastAPI + PyArrow | 8000 |
| Frontend | React + TypeScript + Vite | 5173 |
| Visualization | Rerun SDK (gRPC + Web Viewer) | 9876 / 9090 |

## Prerequisites

- Python 3.10+
- Node.js 18+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- [hf-mount](https://pypi.org/project/hf-mount/) — for mounting HF datasets
- nfs-common (`sudo apt install nfs-common -y`)

## Setup

```bash
# Clone with submodule
git clone --recurse-submodules https://github.com/Phy-Lab-aic/curation-tools.git
cd curation-tools

# If already cloned without submodules
git submodule update --init --recursive

# The converter flow depends on the bundled rosbag2lerobot-svt submodule

# Python environment
uv venv .venv
source .venv/bin/activate
uv pip install fastapi uvicorn pyarrow pydantic-settings rerun-sdk numpy

# Optional: for video frame extraction in Rerun
uv pip install opencv-python

# Frontend dependencies
cd frontend && npm install && cd ..
```

## Usage

### Quick Start

```bash
./start.sh
```

This starts all three services. Open `http://localhost:5173` in your browser.

### Manual Start

```bash
# Terminal 1: Backend
source .venv/bin/activate
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2: Frontend
cd frontend && npm run dev
```

### Production-style Docker Run

```bash
docker compose -f docker/ui/docker-compose.yml up --build -d
```

Open `http://localhost:18080`.

Notes:
- `nginx` serves the frontend bundle and proxies `/api/*` to the FastAPI app.
- `app` runs FastAPI only; it is not exposed directly on the host.
- Converter control remains outside this stack because it still depends on host Docker access.
- If `18080` is already in use, run with `CURATION_UI_PORT=28080 docker compose -f docker/ui/docker-compose.yml up --build -d`.

### Workflow

1. **Load Dataset** — Enter the local path to a LeRobot v3.0 dataset and click "Load"
2. **Browse Episodes** — The episode list appears in the left sidebar with grade badges
3. **Visualize** — Click an episode to view it in the Rerun viewer (center panel)
4. **Grade** — Select a grade (A/B/C/D/F) from the dropdown in the right panel
5. **Tag** — Add tags to categorize episodes (e.g., "good_grasp", "collision", "slow")
6. **Edit Task** — Modify the task instruction text if needed
7. **Save** — Click "Save" to persist changes to the parquet files

All changes are written directly to the dataset's parquet files and survive application restarts.

## Dataset Format

This tool works with LeRobot v3.0 datasets:

```
dataset/
├── meta/
│   ├── info.json                    # Dataset metadata (fps, features, robot_type)
│   ├── tasks.parquet                # Task descriptions (task_index, task)
│   └── episodes/
│       └── chunk-000/
│           └── file-000.parquet     # Episode metadata (+ grade, tags after curation)
├── data/
│   └── chunk-000/
│       └── file-000.parquet         # Observation/action data
└── videos/
    └── observation.images.*/
        └── chunk-000/
            └── file-000.mp4         # Camera recordings
```

### What Gets Modified

| File | Changes | How |
|------|---------|-----|
| `meta/episodes/chunk-*/file-*.parquet` | `grade` and `tags` columns added | New columns appended, original data untouched |
| `meta/tasks.parquet` | `task` column updated | Existing row modified in-place |

Original observation/action data in `data/` and `videos/` is **never modified**.

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/datasets/load` | Load dataset from local path |
| GET | `/api/datasets/info` | Get current dataset metadata |
| GET | `/api/episodes` | List all episodes with grade/tags |
| GET | `/api/episodes/{index}` | Get single episode |
| PATCH | `/api/episodes/{index}` | Update grade and/or tags |
| GET | `/api/tasks` | List all tasks |
| PATCH | `/api/tasks/{index}` | Update task instruction |
| POST | `/api/rerun/visualize/{index}` | Visualize episode in Rerun |
| GET | `/api/health` | Health check |

## Configuration

Environment variables (prefix `CURATION_`):

| Variable | Default | Description |
|----------|---------|-------------|
| `CURATION_FASTAPI_PORT` | 8000 | Backend API port |
| `CURATION_RERUN_GRPC_PORT` | 9876 | Rerun gRPC server port |
| `CURATION_RERUN_WEB_PORT` | 9090 | Rerun web viewer port |

## Development

```bash
# Run backend with auto-reload
source .venv/bin/activate
uvicorn backend.main:app --reload

# Run frontend with HMR
cd frontend && npm run dev

# Build frontend for production
cd frontend && npm run build
```

## HuggingFace Dataset Mounting

Auto-mount all HuggingFace repos (models, datasets, spaces) from the [Phy-lab](https://huggingface.co/Phy-lab) organization to the local filesystem.

### Mount Location

```
/tmp/hf-mounts/Phy-lab/
├── model/<model-name>/
├── dataset/<dataset-name>/
└── space/<space-name>/
```

### Manual Run

```bash
sudo python3 scripts/hf_auto_mount.py

# For private repos
sudo HF_TOKEN=<token> python3 scripts/hf_auto_mount.py
```

Already-mounted repos are automatically skipped.

### Auto-Mount on Boot (systemd)

```bash
sudo cp scripts/hf-auto-mount.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hf-auto-mount.service
```

### Mount Management

| Command | Description |
|---------|-------------|
| `hf-mount status` | Check current mount status |
| `sudo hf-mount stop <mount-path>` | Unmount a specific repo |
| `sudo systemctl restart hf-auto-mount` | Restart service (remount all) |

### Notes

- CPU idle at 0%, active only on file access
- ~20MB memory per mounted repo
- Auto-remounts on reboot when systemd service is enabled
- Run the script again to pick up newly added repos

## Technical Notes

- **Data integrity**: All parquet writes are atomic (temp file + rename). Per-file asyncio locks prevent concurrent write corruption.
- **Rerun API**: Uses `rr.serve_grpc()` + `rr.serve_web_viewer()` (non-deprecated API as of Rerun 0.24+).
- **Episode lookup**: O(1) via episode-to-file index built from `meta/episodes/` metadata on dataset load.
- **Video extraction**: Uses OpenCV (`cv2`) when available; gracefully skips if not installed.

## License

MIT
