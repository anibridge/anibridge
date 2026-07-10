"""Development scripts for AniBridge."""

import argparse
import subprocess
import sys
import time
from pathlib import Path

from anibridge.scripts import __file__ as scripts_file

if scripts_file is None:
    raise RuntimeError("Cannot determine scripts file path.")


def _resolve_root_dir() -> Path:
    """Resolve repository root for script command execution."""
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate

    return Path(scripts_file).resolve().parents[3]


ROOT_DIR = _resolve_root_dir()


def _iter_package_dirs() -> list[Path]:
    """Return nested package repositories that expose a pyproject."""
    packages_dir = ROOT_DIR / "packages"
    if not packages_dir.exists():
        return []

    return sorted(
        path for path in packages_dir.iterdir() if (path / "pyproject.toml").exists()
    )


def _iter_testable_package_dirs() -> list[Path]:
    """Return nested package repositories that contain a tests directory."""
    return [path for path in _iter_package_dirs() if (path / "tests").is_dir()]


def _run_python_command(
    cmd: list[str], *, cwd: Path, success_message: str, failure_message: str
) -> None:
    """Run a Python-related command with consistent error reporting."""
    try:
        subprocess.run(cmd, cwd=cwd, check=True)
        print_success(success_message)
    except subprocess.CalledProcessError:
        print_error(failure_message)
        raise
    except FileNotFoundError as e:
        print_error(f"Tool not found: {e}")
        raise


def _python_tool(name: str) -> str:
    """Resolve a Python tool from the active environment when possible."""
    candidate = Path(sys.executable).with_name(name)
    if candidate.exists():
        return str(candidate)
    return name


class Colors:
    """ANSI color codes for terminal output."""

    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    PURPLE = "\033[95m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    END = "\033[0m"


def print_task(message: str) -> None:
    """Print a task message with formatting."""
    print(
        f"{Colors.CYAN}{Colors.BOLD}[DEV]{Colors.END} {Colors.BLUE}{message}"
        f"{Colors.END}"
    )


def print_success(message: str) -> None:
    """Print a success message with formatting."""
    print(
        f"{Colors.CYAN}{Colors.BOLD}[DEV]{Colors.END} {Colors.GREEN}✓ {message}"
        f"{Colors.END}"
    )


def print_error(message: str) -> None:
    """Print an error message with formatting."""
    print(
        f"{Colors.CYAN}{Colors.BOLD}[DEV]{Colors.END} {Colors.RED}✗ {message}"
        f"{Colors.END}"
    )


def print_info(message: str) -> None:
    """Print an info message with formatting."""
    print(
        f"{Colors.CYAN}{Colors.BOLD}[DEV]{Colors.END} {Colors.YELLOW}→ {message}"
        f"{Colors.END}"
    )


def parse_target_args(argv=None) -> str:
    """Parse command line arguments for target selection."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "target",
        nargs="?",
        default="both",
        choices=["both", "backend", "frontend"],
        help="Target to operate on: both, backend, or frontend",
    )
    args = parser.parse_args(argv)
    return args.target


def build() -> None:
    """Build the application."""
    target = parse_target_args()

    if target == "backend":
        print_info("Backend doesn't require a build step.")
        return

    print_task(f"Building {target}...")

    if target in ("both", "frontend"):
        try:
            print_info("Building frontend...")
            subprocess.run(["pnpm", "build"], cwd=ROOT_DIR / "frontend", check=True)
            print_success("Frontend build completed successfully!")
        except subprocess.CalledProcessError:
            print_error("Frontend build failed!")
            raise
        except FileNotFoundError:
            print_error("pnpm not found! Please install pnpm first.")
            raise


def clean() -> None:
    """Clean build artifacts and cache."""
    target = parse_target_args()

    print_task(f"Cleaning {target}...")

    try:
        if target in ("both", "backend"):
            print_info("Cleaning Python cache and build artifacts...")
            subprocess.run(
                [
                    "find",
                    ".",
                    "-type",
                    "d",
                    "-name",
                    "__pycache__",
                    "-print",
                    "-exec",
                    "rm",
                    "-rf",
                    "{}",
                    "+",
                ],
                cwd=ROOT_DIR,
            )
            subprocess.run(["find", ".", "-name", "*.pyc", "-delete"], cwd=ROOT_DIR)
            print_success("Python artifacts cleaned!")

        if target in ("both", "frontend"):
            print_info("Cleaning frontend build artifacts...")
            subprocess.run(["pnpm", "clean"], cwd=ROOT_DIR / "frontend", check=True)
            print_success("Frontend artifacts cleaned!")

        print_success(f"{target.capitalize()} cleaning completed!")
    except subprocess.CalledProcessError as e:
        print_error(f"Cleaning failed: {e}")
        sys.exit(1)
    except FileNotFoundError as e:
        print_error(f"Tool not found: {e}")
        sys.exit(1)


def deps_install() -> None:
    """Install dependencies."""
    target = parse_target_args()

    print_task(f"Installing {target} dependencies...")

    try:
        if target in ("both", "backend"):
            print_info("Installing Python dependencies...")
            subprocess.run(
                ["uv", "sync", "--all-extras", "--all-groups", "--all-packages"],
                cwd=ROOT_DIR,
                check=True,
            )
            print_success("Python dependencies installed successfully!")

        if target in ("both", "frontend"):
            print_info("Installing frontend dependencies...")
            subprocess.run(["pnpm", "install"], cwd=ROOT_DIR / "frontend", check=True)
            print_success("Frontend dependencies installed successfully!")

    except subprocess.CalledProcessError as e:
        print_error(f"Dependency installation failed: {e}")
        sys.exit(1)
    except FileNotFoundError as e:
        print_error(f"Tool not found: {e}")
        sys.exit(1)


def deps_upgrade() -> None:
    """Upgrade dependencies."""
    target = parse_target_args()

    print_task(f"Upgrading {target} dependencies...")

    try:
        if target in ("both", "backend"):
            print_info("Upgrading Python dependencies...")
            subprocess.run(
                [
                    "uv",
                    "sync",
                    "--upgrade",
                    "--all-extras",
                    "--all-groups",
                    "--all-packages",
                ],
                cwd=ROOT_DIR,
                check=True,
            )
            print_success("Python dependencies upgraded successfully!")

        if target in ("both", "frontend"):
            print_info("Upgrading frontend dependencies...")
            subprocess.run(["pnpm", "update"], cwd=ROOT_DIR / "frontend", check=True)
            print_success("Frontend dependencies upgraded successfully!")

    except subprocess.CalledProcessError as e:
        print_error(f"Dependency upgrade failed: {e}")
        sys.exit(1)
    except FileNotFoundError as e:
        print_error(f"Tool not found: {e}")
        sys.exit(1)


def dev() -> None:
    """Run the development environment."""
    target = parse_target_args()

    print_task("Starting development environment...")

    procs = []

    if target in ("both", "backend"):
        print_info("Starting backend server...")
        procs.append(subprocess.Popen(["python3", "main.py"], cwd=ROOT_DIR))

    if target in ("both", "frontend"):
        print_info("Starting frontend development server...")
        procs.append(subprocess.Popen(["pnpm", "dev"], cwd=ROOT_DIR / "frontend"))

    if not procs:
        print_error(f"No servers to start for target: {target}")
        return

    server_names = {
        "both": "Development servers",
        "backend": "Backend server",
        "frontend": "Frontend server",
    }

    print_success(f"{server_names[target]} started!")
    print_info("Press Ctrl+C to stop all servers")

    try:
        while all(p.poll() is None for p in procs):
            time.sleep(0.1)
    except KeyboardInterrupt:
        print_info("Shutting down development servers...")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()

        time.sleep(1)

        for p in procs:
            if p.poll() is None:
                p.kill()

        print_success("Development servers stopped!")


def format_() -> None:
    """Format the codebase."""
    target = parse_target_args()

    print_task(f"Formatting {target}...")

    try:
        if target in ("both", "backend"):
            print_info("Running ruff formatter on Python code...")
            subprocess.run(
                [_python_tool("ruff"), "format", "."], cwd=ROOT_DIR, check=True
            )
            subprocess.run(
                [_python_tool("ruff"), "check", ".", "--fix"], cwd=ROOT_DIR, check=True
            )
            print_success("Python code formatted successfully!")

            for package_dir in _iter_package_dirs():
                print_info(f"Running ruff formatter in {package_dir.name}...")
                subprocess.run(
                    [_python_tool("ruff"), "format", "."], cwd=package_dir, check=True
                )
                subprocess.run(
                    [_python_tool("ruff"), "check", ".", "--fix"],
                    cwd=package_dir,
                    check=True,
                )
                print_success(f"{package_dir.name} formatted successfully!")

        if target in ("both", "frontend"):
            print_info("Running pnpm format on frontend code...")
            subprocess.run(["pnpm", "format"], cwd=ROOT_DIR / "frontend", check=True)
            print_success("Frontend code formatted successfully!")

        print_success(f"{target.capitalize()} formatting completed!")
    except subprocess.CalledProcessError:
        print_error("Code formatting failed!")
        raise
    except FileNotFoundError as e:
        print_error(f"Tool not found: {e}")
        raise


def lint() -> None:
    """Lint the codebase."""
    target = parse_target_args()

    print_task(f"Linting {target}...")

    try:
        if target in ("both", "backend"):
            print_info("Running ruff linter and ty type checker on Python code...")
            subprocess.run(
                [_python_tool("ruff"), "check", "."], cwd=ROOT_DIR, check=True
            )
            subprocess.run(
                [_python_tool("ruff"), "format", "--check", "."],
                cwd=ROOT_DIR,
                check=True,
            )
            subprocess.run([_python_tool("ty"), "check", "."], cwd=ROOT_DIR, check=True)
            print_success("Python code linting passed!")

            for package_dir in _iter_package_dirs():
                print_info(f"Running lint checks in {package_dir.name}...")
                subprocess.run(
                    [_python_tool("ruff"), "check", "."], cwd=package_dir, check=True
                )
                subprocess.run(
                    [_python_tool("ruff"), "format", "--check", "."],
                    cwd=package_dir,
                    check=True,
                )
                subprocess.run(
                    [_python_tool("ty"), "check", "."], cwd=package_dir, check=True
                )
                print_success(f"{package_dir.name} linting passed!")

        if target in ("both", "frontend"):
            print_info("Running pnpm lint on frontend code...")
            subprocess.run(["pnpm", "lint"], cwd=ROOT_DIR / "frontend", check=True)
            print_success("Frontend code linting passed!")

        print_success(f"{target.capitalize()} linting checks completed successfully!")
    except subprocess.CalledProcessError:
        print_error("Linting failed! Please fix the issues above.")
        sys.exit(1)
    except FileNotFoundError as e:
        print_error(f"Tool not found: {e}")
        sys.exit(1)


def meta_setup() -> None:
    """Setup the meta-repo.

    Clones/updates all AniBridge repos into ./packages and installs them editable into
    the current environment.
    """
    parser = argparse.ArgumentParser(description="Setup AniBridge meta-repo workspace.")
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="Clone/update repos but skip editable installs.",
    )
    args, _ = parser.parse_known_args()

    packages_dir = ROOT_DIR / "packages"
    packages_dir.mkdir(parents=True, exist_ok=True)

    repos: dict[str, str] = {
        "anibridge-anilist-provider": "https://github.com/anibridge/anibridge-anilist-provider",
        "anibridge-emby-provider": "https://github.com/anibridge/anibridge-emby-provider",
        "anibridge-jellyfin-provider": "https://github.com/anibridge/anibridge-jellyfin-provider",
        "anibridge-mal-provider": "https://github.com/anibridge/anibridge-mal-provider",
        "anibridge-plex-provider": "https://github.com/anibridge/anibridge-plex-provider",
        "anibridge-provider-base": "https://github.com/anibridge/anibridge-provider-base",
        "anibridge-simkl-provider": "https://github.com/anibridge/anibridge-simkl-provider",
        "anibridge-trakt-provider": "https://github.com/anibridge/anibridge-trakt-provider",
        "anibridge-utils": "https://github.com/anibridge/anibridge-utils",
    }

    print_task(f"Setting up meta workspace in {packages_dir} ...")

    def _run(cmd: list[str], *, cwd: Path | None = None) -> None:
        try:
            subprocess.run(cmd, cwd=cwd or ROOT_DIR, check=True)
        except subprocess.CalledProcessError as e:
            print_error(f"Command failed: {' '.join(cmd)}")
            raise e

    for name, url in repos.items():
        dest = packages_dir / name

        if (dest / ".git").exists():
            print_info(f"Updating {name}...")
            _run(["git", "fetch", "--all", "--prune"], cwd=dest)
            try:
                _run(["git", "pull", "--ff-only"], cwd=dest)
            except subprocess.CalledProcessError:
                print_error(f"Failed to update {name}.")
        else:
            print_info(f"Cloning {name}...")
            _run(["git", "clone", url, str(dest)], cwd=packages_dir)

    print_success("Repos cloned/updated.")

    if args.no_install:
        print_info("Skipping editable installs (--no-install).")
        return

    print_task("Installing repos editable into current environment...")

    for name in repos:
        dest = packages_dir / name
        if (dest / "pyproject.toml").exists():
            print_info(f"uv pip install -e {dest} ...")
            _run(["uv", "pip", "install", "-e", str(dest)], cwd=ROOT_DIR)
        else:
            print_info(f"Skipping {name}: no pyproject.toml found at repo root.")

    print_success("Meta workspace is ready! All packages installed from local source.")


def test() -> None:
    """Run the backend test suite."""
    target = parse_target_args()

    if target == "frontend":
        print_error("Frontend does not have a pytest suite to run.")
        sys.exit(1)

    print_task("Running backend tests with pytest...")

    try:
        subprocess.run([sys.executable, "-m", "pytest"], cwd=ROOT_DIR, check=True)
        print_success("Pytest suite completed successfully!")

        for package_dir in _iter_testable_package_dirs():
            print_info(f"Running pytest in {package_dir.name}...")
            _run_python_command(
                [sys.executable, "-m", "pytest"],
                cwd=package_dir,
                success_message=f"{package_dir.name} tests completed successfully!",
                failure_message=f"{package_dir.name} tests failed!",
            )
    except subprocess.CalledProcessError:
        print_error("Tests failed! Please fix the issues above.")
        sys.exit(1)
    except FileNotFoundError as e:
        print_error(f"Tool not found: {e}")
        sys.exit(1)


def start() -> None:
    """Start the AniBridge application."""
    target = parse_target_args()

    if target == "frontend":
        print_error("Frontend cannot be started independently in production mode.")
        print_info(
            "Use 'build' to create a production build, or 'dev frontend' for "
            "development."
        )
        sys.exit(1)

    print_task("Starting AniBridge application...")

    try:
        subprocess.run(["python3", "main.py"], cwd=ROOT_DIR, check=True)
        print_success("Application started successfully!")
    except subprocess.CalledProcessError as e:
        print_error(f"Application failed to start: {e}")
        sys.exit(1)
    except FileNotFoundError:
        print_error(
            "Python3 not found! Please ensure Python 3 is installed and in your PATH."
        )
        sys.exit(1)


def main() -> None:
    """Main entry point for direct script execution."""
    parser = argparse.ArgumentParser(description="Development scripts for AniBridge.")
    parser.add_argument(
        "command",
        choices=[
            "build",
            "clean",
            "deps-install",
            "deps-upgrade",
            "dev",
            "format",
            "lint",
            "meta-setup",
            "test",
            "start",
        ],
        help="Command to execute",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="both",
        choices=["both", "backend", "frontend"],
        help="Target to operate on: both, backend, or frontend",
    )
    args = parser.parse_args()

    sys.argv = [sys.argv[0], args.target]  # Pass target to functions

    match args.command:
        case "build":
            build()
        case "clean":
            clean()
        case "deps-install":
            deps_install()
        case "deps-upgrade":
            deps_upgrade()
        case "dev":
            dev()
        case "format":
            format_()
        case "lint":
            lint()
        case "meta-setup":
            meta_setup()
        case "test":
            test()
        case "start":
            start()
        case _:
            print_error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
