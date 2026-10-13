"""在本机交互式填写百炼密钥；不依赖额外库，不打印密钥。"""
from getpass import getpass
from pathlib import Path
import sys


def main():
    if not sys.stdin.isatty():
        print("请在你自己的 PowerShell 窗口中直接运行本脚本。")
        return 1
    folder = Path(__file__).absolute().parent
    env_path = folder / ".env"
    template = folder / ".env.aliyun.example"
    if not env_path.exists() and not template.exists():
        print("未找到配置模板，请在 CampusFlow-A 项目文件夹中运行。")
        return 1
    try:
        key = getpass("请粘贴百炼 API Key，然后按回车（输入不会显示）：").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n已取消，配置未修改。")
        return 1
    if not key or any(char.isspace() for char in key) or any(char in key for char in "\"'#$"):
        print("没有保存：密钥为空或含有多余字符，请复制完整的 API Key 后重试。")
        return 1
    original = (env_path if env_path.exists() else template).read_text(encoding="utf-8-sig")
    lines = original.splitlines()
    replacement = "LLM_API_KEY=" + key
    updated, replaced = [], False
    for line in lines:
        if line.strip().startswith("LLM_API_KEY="):
            if not replaced:
                updated.append(replacement)
                replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(replacement)
    temporary = folder / ".env.tmp"
    try:
        temporary.write_text("\n".join(updated) + "\n", encoding="utf-8")
        temporary.replace(env_path)
    except OSError:
        print("保存失败，请检查项目文件夹是否有写入权限。")
        return 1
    print("密钥已保存到本机 .env，没有显示或上传密钥。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
