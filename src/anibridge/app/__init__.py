"""AniBridge Initialization Module."""

from anibridge.app.config.settings import get_config
from anibridge.app.utils.logging import get_logger
from anibridge.app.utils.terminal import supports_utf8
from anibridge.app.utils.version import (
    get_docker_status,
    get_git_hash,
    get_pyproject_version,
)

__author__ = "Elias Benbourenane <eliasbenbourenane@gmail.com>"
__credits__ = ["eliasbenb"]
__license__ = "MIT"
__maintainer__ = "eliasbenb"
__email__ = "eliasbenbourenane@gmail.com"
__version__ = get_pyproject_version()
__git_hash__ = get_git_hash()


config = get_config()
log = get_logger()

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
