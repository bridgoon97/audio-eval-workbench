"""独立 Agent CLI 入口：只提供评测任务编排子命令，不启动服务端。

与 `workbench.cli` 共享同一套 agent 子命令实现（workbench.agent_cli），
业务逻辑全部位于 workbench.agent_tasks；本模块不含任何编排逻辑。
版本与服务器端同源（workbench.version.VERSION），apply 会对服务版本做一致性校验。
"""

import argparse
import sys

from workbench import agent_cli
from workbench.version import VERSION

BANNER = f"""听鉴 · 独立 Agent CLI（版本 {VERSION}）

这是评测任务编排工具，不包含评测服务端：
- 服务端请在组织者的电脑上运行（完整版 audio-eval.exe）。
- 本工具负责 manifest 校验、素材准备与通过 HTTP API 的幂等编排。
- 详细手册见同目录 docs/Agent创建评测任务.md；协作规则见同目录 AGENTS.md。
"""


def main() -> int:
    # Windows 重定向输出可能采用 cp1252；中文输出必须可编码。
    # 此处内联等价逻辑，避免 import workbench.cli 把服务端依赖带进分析图。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    argv = sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog="audio-eval-agent",
        description="听鉴独立 Agent CLI · 评测任务编排（不含服务端）",
    )
    agent_cli.register(parser, entry_program="audio-eval-agent")
    if not argv:
        # 双击/无参数：显示帮助并停留，不静默退出，也不监听任何端口。
        print(BANNER)
        parser.print_help()
        print("\n未提供子命令；常用入口：manifest-init / validate / prepare / apply / status")
        try:
            input("\n按回车键退出…")
        except EOFError:
            pass
        return 0
    args = parser.parse_args(argv)
    return agent_cli.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
