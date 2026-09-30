"""一键换机引擎：识别、打包、校验、迁移被 git 排除但运行必要的数据。

唯一副本位于 one-click-migrate 技能目录；业务项目通过 .migrate/manifest.toml
声明差异，不复制引擎。
"""

__version__ = "1.0.0"

#: 引擎要求的最低 Python 版本（tomllib 自 3.11 起进入标准库）。
MIN_PYTHON = (3, 11)

#: 退出码契约。scripts/migrate.cmd 等启动器按这些码分支，属于工具接口的一部分。
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFLICTS = 3
EXIT_PASSPHRASE = 4
EXIT_INTEGRITY = 5
EXIT_PLATFORM = 6
EXIT_REFUSED = 7
EXIT_UNSUPPORTED = 8
