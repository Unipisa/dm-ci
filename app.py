import asyncio
import configparser
import json
import logging
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request

CONFIG_PATH = Path(__file__).parent / "config.ini"
QUEUE_CAPACITY = 100
COMMAND_TIMEOUT = 600

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

config = configparser.ConfigParser()
config.read(CONFIG_PATH)

if "folders" not in config:
    raise RuntimeError("config.ini must contain a [folders] section with API keys and project directories")

folders = dict(config["folders"])


@dataclass(frozen=True)
class UpdateJob:
    job_id: str
    project_path: Path


def extract_pulled_images(output: str) -> list[str]:
    """Return image references with a completed pull event, not necessarily changed images."""
    images = []
    seen = set()
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        resource = event.get("id")
        if (
            not isinstance(resource, str)
            or not resource.startswith("Image ")
            or event.get("status") != "Done"
            or event.get("text") != "Pulled"
        ):
            continue
        image = resource.removeprefix("Image ").strip()
        if image and image not in seen:
            seen.add(image)
            images.append(image)
    return images


def check_compose_file(project_path: Path) -> None:
    if not (project_path / "docker-compose.yml").is_file():
        raise FileNotFoundError(
            f"docker-compose.yml not found in configured directory: {project_path}"
        )


def run_compose(job: UpdateJob, *args: str) -> subprocess.CompletedProcess:
    step = " ".join(args)
    logger.info("Job %s: docker compose %s", job.job_id, step)
    try:
        result = subprocess.run(
            ["docker", "compose", *args],
            capture_output=True,
            text=True,
            cwd=job.project_path,
            timeout=COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"docker compose {step} timed out") from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"docker compose {step} failed (exit {result.returncode})\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def perform_update(job: UpdateJob) -> None:
    # The file could have been removed while this job was waiting.
    check_compose_file(job.project_path)

    pull_result = run_compose(job, "--progress", "json", "pull")
    pulled_images = extract_pulled_images(pull_result.stdout + "\n" + pull_result.stderr)
    if pulled_images:
        logger.info("Job %s completed pull operations for images: %s", job.job_id, ", ".join(pulled_images))
    else:
        logger.info("Job %s: no completed image pull events detected", job.job_id)

    run_compose(job, "up", "-d")


async def update_worker(queue: asyncio.Queue[UpdateJob | None]) -> None:
    while True:
        job = await queue.get()
        try:
            if job is None:
                return

            logger.info("Job %s started project=%s", job.job_id, job.project_path)
            await asyncio.to_thread(perform_update, job)
            logger.info("Job %s completed project=%s", job.job_id, job.project_path)
        except Exception:
            logger.exception("Update job %s failed project=%s", job.job_id, job.project_path)
        finally:
            queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    queue: asyncio.Queue[UpdateJob | None] = asyncio.Queue(maxsize=QUEUE_CAPACITY)
    app.state.update_queue = queue
    app.state.accepting_updates = True
    worker = asyncio.create_task(update_worker(queue), name="update-worker")
    try:
        yield
    finally:
        app.state.accepting_updates = False
        # Pending jobs are intentionally not persisted or drained on shutdown.
        # Do not cancel the active to_thread call: it cannot stop its subprocess.
        while not queue.empty():
            job = queue.get_nowait()
            queue.task_done()
            if job is not None:
                logger.info("Job %s discarded during shutdown", job.job_id)
        queue.put_nowait(None)
        await worker


app = FastAPI(title="Custom Script Updater", lifespan=lifespan)


@app.post("/update", status_code=202)
async def update(
    request: Request,
    api_key: str = Query(..., description="API key to select the folder"),
):
    if api_key not in folders:
        raise HTTPException(status_code=401, detail="Invalid API key")

    project_dir = folders[api_key].strip()
    if not project_dir:
        raise HTTPException(status_code=500, detail="No project directory configured for this API key")

    try:
        project_path = (Path(__file__).parent / project_dir).resolve()
        check_compose_file(project_path)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not request.app.state.accepting_updates:
        raise HTTPException(status_code=503, detail="Updater is shutting down")

    job = UpdateJob(job_id=str(uuid4()), project_path=project_path)
    try:
        request.app.state.update_queue.put_nowait(job)
    except asyncio.QueueFull:
        raise HTTPException(status_code=503, detail="Update queue is full") from None

    logger.info("Job %s queued project=%s", job.job_id, job.project_path)
    return {
        "status": "queued",
        "job_id": job.job_id,
        "project_dir": str(job.project_path),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app", host="0.0.0.0", port=8000, log_level="info",
        workers=1, access_log=False,
    )
