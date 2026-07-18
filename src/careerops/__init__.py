from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("careerops")
except PackageNotFoundError:  # pragma: no cover - only possible outside an installed project
    __version__ = "0.0.0"


def main() -> None:
    """Run the loopback-only API process."""
    import uvicorn

    from careerops.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "careerops.api.app:app",
        host=settings.bind_host,
        port=settings.bind_port,
        reload=False,
    )


__all__ = ["__version__", "main"]
