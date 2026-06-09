import configparser
import logging
import re
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

CONFIG_PATH = Path(__file__).parent / "config.ini"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Custom Script Updater")

config = configparser.ConfigParser()
config.read(CONFIG_PATH)

if "folders" not in config:
    raise RuntimeError("config.ini must contain a [folders] section with API keys and project directories")

folders = dict(config["folders"])


def extract_pulled_images(output: str) -> list[str]:
    images = []
    for line in output.splitlines():
        match = re.search(r"Image [^ ]+ \(([^)]+)\) Pulled", line)
        if match:
            image_name = match.group(1)
            if image_name not in images:
                images.append(image_name)
    return images


@app.post("/update")
async def update(api_key: str = Query(..., description="API key to select the folder")):
    if api_key not in folders:
        raise HTTPException(status_code=401, detail="Invalid API key")

    project_dir = folders[api_key].strip()
    if not project_dir:
        raise HTTPException(status_code=500, detail="No project directory configured for this API key")

    project_path = Path(project_dir)
    if not project_path.is_absolute():
        project_path = (Path(__file__).parent / project_path).resolve()

    logger.info("Updating project folder %s for API key %s", project_path, api_key)

    compose_file = project_path / "docker-compose.yml"
    if not compose_file.exists() or not compose_file.is_file():
        raise HTTPException(
            status_code=500,
            detail=f"docker-compose.yml not found in configured directory: {project_path}",
        )

    try:
        pull_result = subprocess.run(
            ["docker", "compose", "pull"],
            capture_output=True,
            text=True,
            cwd=project_path,
            timeout=600,
        )
        if pull_result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail={
                    "step": "pull",
                    "returncode": pull_result.returncode,
                    "stdout": pull_result.stdout,
                    "stderr": pull_result.stderr,
                },
            )

        pulled_images = extract_pulled_images(pull_result.stdout + pull_result.stderr)
        if pulled_images:
            logger.info("Pulled images for %s: %s", project_path, ", ".join(pulled_images))
        else:
            logger.info("No pulled images detected for %s", project_path)

        up_result = subprocess.run(
            ["docker", "compose", "up", "-d"],
            capture_output=True,
            text=True,
            cwd=project_path,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Docker compose command timed out")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return JSONResponse(
        status_code=200 if up_result.returncode == 0 else 500,
        content={
            "api_key": api_key,
            "project_dir": str(project_path),
            "pull": {
                "returncode": pull_result.returncode,
                "stdout": pull_result.stdout,
                "stderr": pull_result.stderr,
            },
            "up": {
                "returncode": up_result.returncode,
                "stdout": up_result.stdout,
                "stderr": up_result.stderr,
            },
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, log_level="info")
