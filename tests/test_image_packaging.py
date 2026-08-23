import shlex
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPOSITORY_ROOT / "Dockerfile"
DOCKERIGNORE = REPOSITORY_ROOT / ".dockerignore"

EXPECTED_COPY_SOURCES = {
    "requirements_client.txt",
    "requirements.txt",
    "src/",
}
EXPECTED_CONTEXT_INCLUDES = {
    "!requirements_client.txt",
    "!requirements.txt",
    "!src/",
    "!src/**",
}
REQUIRED_PRIVATE_PATH_EXCLUDES = {
    "**/.env*",
    "**/.git",
    "**/__pycache__",
    "**/*.py[cod]",
    "**/.cache",
    "**/.pytest_cache",
    "**/.ruff_cache",
    "**/.mypy_cache",
    "**/.uv-cache",
    "**/.hypothesis",
    "**/.tox",
    "**/.nox",
    "**/.venv",
    "**/.direnv",
    "**/venv",
    "**/env",
    "**/*.egg-info",
    "**/outputs",
    "**/user_data",
    "**/live_data",
    "**/tests",
    "**/.coverage*",
    "**/coverage.xml",
    "**/htmlcov",
    "**/test-results",
    "**/junit.xml",
}
RUNTIME_MOUNT_TARGETS = (
    "/app/geolang/outputs",
    "/app/geolang/user_data",
    "/app/geolang/live_data",
    "/app/geolang/natural_earth",
)


def dockerfile_copy_sources() -> tuple[set[str], list[str]]:
    copy_sources = set()
    add_instructions = []

    for line in DOCKERFILE.read_text().splitlines():
        instruction = line.lstrip().partition(" ")[0].upper()
        if instruction not in {"ADD", "COPY"}:
            continue

        arguments = shlex.split(line)
        if instruction == "ADD":
            add_instructions.append(line)
            continue

        paths = [argument for argument in arguments[1:] if not argument.startswith("--")]
        copy_sources.update(paths[:-1])

    return copy_sources, add_instructions


def dockerignore_patterns() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def test_image_recipe_copies_the_complete_runtime_source_only():
    copy_sources, add_instructions = dockerfile_copy_sources()

    assert copy_sources == EXPECTED_COPY_SOURCES
    assert add_instructions == []
    assert (REPOSITORY_ROOT / "src/api/server.py").is_file()
    assert (REPOSITORY_ROOT / "src/api/executor.py").is_file()
    assert (REPOSITORY_ROOT / "src/static/index.html").is_file()


def test_image_recipe_creates_uid_1000_runtime_mount_targets_before_source():
    recipe = DOCKERFILE.read_text()
    mount_targets = " ".join(RUNTIME_MOUNT_TARGETS)

    assert f"mkdir -p {mount_targets}" in recipe
    assert f"chown 1000:1000 {mount_targets}" in recipe
    assert "chmod 777 /app/geolang/outputs" in recipe
    assert recipe.index("chown 1000:1000") < recipe.index("COPY src/ ./src/")


def test_build_context_cannot_include_private_or_runtime_data():
    patterns = dockerignore_patterns()
    context_includes = {pattern for pattern in patterns if pattern.startswith("!")}

    assert patterns[0] == "**"
    assert context_includes == EXPECTED_CONTEXT_INCLUDES
    assert REQUIRED_PRIVATE_PATH_EXCLUDES <= set(patterns)

    last_include = max(
        index for index, pattern in enumerate(patterns) if pattern.startswith("!")
    )
    first_private_exclude = min(
        patterns.index(pattern) for pattern in REQUIRED_PRIVATE_PATH_EXCLUDES
    )
    assert first_private_exclude > last_include
