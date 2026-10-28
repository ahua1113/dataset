"""V6.4 采集器的兼容导出入口。"""

# 统一导出旧接口，避免既有脚本因目录重构失效。
from .constants import *  # noqa: F401,F403
from .models import Journal, load_journals
from .client import Client, SourceError
from .utils import *  # noqa: F401,F403
from .disciplines import *  # noqa: F401,F403
from .discovery import *  # noqa: F401,F403
from .storage import *  # noqa: F401,F403
from .reports import *  # noqa: F401,F403
from .policy import *  # noqa: F401,F403
from .feasibility import collect_papers
from .papers import collect_papers_v6_4
from .resources import *  # noqa: F401,F403

