"""AniBridge Initialization Module."""

from typing import TYPE_CHECKING

from anibridge.utils.cache import set_default_cache_dir

from anibridge.app.utils.terminal import supports_utf8
from anibridge.app.utils.version import (
    get_docker_status,
    get_git_hash,
    get_pyproject_version,
)

if TYPE_CHECKING:
    from anibridge.app.config.settings import AnibridgeConfig

__author__ = "Elias Benbourenane <eliasbenbourenane@gmail.com>"
__credits__ = ["eliasbenb"]
__license__ = "MIT"
__maintainer__ = "eliasbenb"
__email__ = "eliasbenbourenane@gmail.com"
__version__ = get_pyproject_version()
__git_hash__ = get_git_hash()

if supports_utf8():
    ANIBDRIGE_HEADER = f"""
╔═══════════════════════════════════════════════════════════════════════════════╗
║                               A N I B R I D G E                               ║
╠═══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║  Version: {__version__:<68}║
║  Git Hash: {__git_hash__:<67}║
║  Docker: {"Yes" if get_docker_status() else "No":<69}║
║  Author: {f"{__author__} @{__maintainer__}":<69}║
║  License: {__license__:<68}║
║  Repository: https://github.com/anibridge/anibridge                           ║
║  Documentation: https://anibridge.eliasbenb.dev                               ║
║                                                                               ║
╚═══════════════════════════════════════════════════════════════════════════════╝""".strip()
else:
    ANIBDRIGE_HEADER = f"""
+-------------------------------------------------------------------------------+
|                               A N I B R I D G E                               |
+-------------------------------------------------------------------------------+
|                                                                               |
|  Version: {__version__:<68}|
|  Git Hash: {__git_hash__:<67}|
|  Docker: {"Yes" if get_docker_status() else "No":<69}|
|  Author: {f"{__author__} @{__maintainer__}":<69}|
|  License: {__license__:<68}|
|  Repository: https://github.com/anibridge/anibridge                           |
|  Documentation: https://anibridge.eliasbenb.dev                               |
|                                                                               |
+-------------------------------------------------------------------------------+""".strip()


def initialize_runtime() -> AnibridgeConfig:
    """Initialize runtime paths that depend on the resolved config."""
    from anibridge.app.config.settings import get_config

    config = get_config()
    set_default_cache_dir(config.data_path / ".cache")
    return config
