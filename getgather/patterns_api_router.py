import os
import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from loguru import logger
from pydantic import BaseModel

from getgather.zen_distill import load_distillation_patterns

router = APIRouter()

PATTERNS_DIR = os.path.join(os.path.dirname(__file__), "mcp", "patterns")

_VALID_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")
_SUPPORTED_EXTENSIONS = (".html", ".json")
_MEDIA_TYPES: dict[str, str] = {".html": "text/html", ".json": "application/json"}


def _validate_name(pattern_name: str) -> None:
    if not _VALID_NAME.match(pattern_name):
        raise HTTPException(status_code=400, detail="Invalid pattern name")


def _validate_ext(ext: str) -> str:
    suffix = f".{ext}"
    if suffix not in _SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid extension '.{ext}'. Must be one of: {', '.join(_SUPPORTED_EXTENSIONS)}",
        )
    return suffix


def _pattern_path(pattern_name: str, suffix: str) -> str:
    return os.path.join(PATTERNS_DIR, f"{pattern_name}{suffix}")


class PatternBody(BaseModel):
    content: str


@router.get("/api/v1/patterns")
async def list_patterns() -> JSONResponse:
    logger.info("Listing all patterns...")
    try:
        names = sorted(
            f
            for f in os.listdir(PATTERNS_DIR)
            if any(f.endswith(ext) for ext in _SUPPORTED_EXTENSIONS)
        )
        return JSONResponse(names)
    except Exception as e:
        detail = "Unable to list patterns"
        logger.error(f"{detail} Exception={e}")
        raise HTTPException(status_code=500, detail=detail)


@router.get("/api/v1/patterns/{pattern_name}")
async def get_pattern(pattern_name: str, ext: str = Query("html")) -> PlainTextResponse:
    _validate_name(pattern_name)
    suffix = _validate_ext(ext)
    path = _pattern_path(pattern_name, suffix)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"Pattern {pattern_name!r} not found")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return PlainTextResponse(content, media_type=_MEDIA_TYPES[suffix])
    except Exception as e:
        detail = f"Unable to read pattern {pattern_name!r}"
        logger.error(f"{detail} Exception={e}")
        raise HTTPException(status_code=500, detail=detail)


@router.post("/api/v1/patterns/{pattern_name}")
async def upsert_pattern(
    pattern_name: str, body: PatternBody, ext: str = Query("html")
) -> dict[str, str]:
    _validate_name(pattern_name)
    suffix = _validate_ext(ext)
    path = _pattern_path(pattern_name, suffix)
    exists = os.path.isfile(path)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(body.content)
        load_distillation_patterns.cache_clear()
        action = "updated" if exists else "created"
        logger.info(f"Pattern {pattern_name!r} {action}.")
        return {"pattern_name": pattern_name, "status": action}
    except Exception as e:
        detail = f"Unable to write pattern {pattern_name!r}"
        logger.error(f"{detail} Exception={e}")
        raise HTTPException(status_code=500, detail=detail)


@router.delete("/api/v1/patterns/{pattern_name}")
async def delete_pattern(pattern_name: str, ext: str = Query("html")) -> dict[str, str]:
    _validate_name(pattern_name)
    suffix = _validate_ext(ext)
    path = _pattern_path(pattern_name, suffix)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"Pattern {pattern_name!r} not found")
    try:
        os.remove(path)
        load_distillation_patterns.cache_clear()
        logger.info(f"Pattern {pattern_name!r} deleted.")
        return {"pattern_name": pattern_name, "status": "deleted"}
    except Exception as e:
        detail = f"Unable to delete pattern {pattern_name!r}"
        logger.error(f"{detail} Exception={e}")
        raise HTTPException(status_code=500, detail=detail)
